"""
Grid strategy backtester — Skill 7 (hftbacktest-inspired).

A purpose-built grid trading simulator that models:
  - Individual order placement and fill sequencing
  - POST_ONLY maker fee (0.25% per leg)
  - Grid recentring when price moves > config.range_pct from calibration
  - Capital reservation (capital_pct ≤ 0.80)
  - Fixed-level grid topology matching the live bot:
      · After a BUY fills: re-place same BUY; track fill price in FIFO queue
      · After a SELL fills: re-place same SELL; P&L paired with oldest BUY
  - Daily P&L, weekly Sharpe, max drawdown, win rate

Unlike the simple simulate_return() in gemini_optimizer.py this backtester
steps through each candle in order, maintains open order state, and counts
only fills that are geometrically reachable given the day's OHLC range.

Usage
-----
  # Quick backtest with current config:
  python3 src/backtesting/grid_backtest.py

  # Custom params:
  python3 src/backtesting/grid_backtest.py --spacing 1.0 --levels 6 --capital 0.65 --days 90

  # From gemini_optimizer:
  from backtesting.grid_backtest import run_backtest, BacktestResult
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


# ------------------------------------------------------------------ #
#  Configuration                                                       #
# ------------------------------------------------------------------ #

MAKER_FEE   = 0.0025   # 0.25% per leg
MIN_QTY_BTC = 0.0001
# RECENTER_THRESHOLD_PCT removed — use config.range_pct so backtest matches live bot


@dataclass
class GridConfig:
    spacing_pct:   float = 1.0   # updated to match new default (was 0.8)
    range_pct:     float = 5.0   # also used as the recenter threshold
    levels:        int   = 6     # updated to match new default (was 10)
    capital_pct:   float = 0.70
    total_capital: float = 150.0
    gbp_usd_rate:  float = 1.27
    kill_pct:      float = 0.10

    @classmethod
    def from_file(cls, path: Path) -> "GridConfig":
        if path.exists():
            d = json.loads(path.read_text())
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        return cls()

    @property
    def capital_usdt(self) -> float:
        return self.total_capital * self.capital_pct * self.gbp_usd_rate

    @property
    def per_level_usdt(self) -> float:
        return self.capital_usdt / self.levels if self.levels else 0


# ------------------------------------------------------------------ #
#  Order model                                                         #
# ------------------------------------------------------------------ #

@dataclass
class Order:
    level:     int
    side:      str      # BUY | SELL
    price:     float
    qty:       float
    buy_price: Optional[float] = None   # for SELL orders: the matched BUY price


# ------------------------------------------------------------------ #
#  Result                                                              #
# ------------------------------------------------------------------ #

@dataclass
class BacktestResult:
    total_trades:    int   = 0
    total_sells:     int   = 0
    wins:            int   = 0
    total_gross_gbp: float = 0.0
    total_fees_gbp:  float = 0.0
    total_net_gbp:   float = 0.0
    max_drawdown_gbp: float = 0.0
    daily_pnl:       list  = field(default_factory=list)
    recenter_count:  int   = 0

    @property
    def win_rate_pct(self) -> float:
        return round(self.wins / self.total_sells * 100, 1) if self.total_sells else 0.0

    @property
    def sharpe(self) -> float:
        if len(self.daily_pnl) < 2:
            return 0.0
        mean = sum(self.daily_pnl) / len(self.daily_pnl)
        var  = sum((x - mean) ** 2 for x in self.daily_pnl) / len(self.daily_pnl)
        std  = math.sqrt(var)
        return round(mean / std, 3) if std else 0.0

    @property
    def fee_drag_pct(self) -> float:
        if self.total_gross_gbp <= 0:
            return 0.0
        return round(self.total_fees_gbp / self.total_gross_gbp * 100, 1)

    def summary(self) -> str:
        return (
            f"Trades: {self.total_trades} | Sells: {self.total_sells} | "
            f"Win rate: {self.win_rate_pct}%\n"
            f"Net P&L: £{self.total_net_gbp:.2f} | "
            f"Gross: £{self.total_gross_gbp:.2f} | "
            f"Fees: £{self.total_fees_gbp:.2f}\n"
            f"Max drawdown: £{self.max_drawdown_gbp:.2f} | "
            f"Sharpe: {self.sharpe} | "
            f"Fee drag: {self.fee_drag_pct}%\n"
            f"Recentres: {self.recenter_count}"
        )


# ------------------------------------------------------------------ #
#  Grid builder                                                        #
# ------------------------------------------------------------------ #

_REGIME_GRID_SKEW = {
    "trending_up": 0.60,
    "trending_dn": 0.40,
    "ranging":     0.50,
    "volatile":    0.50,
}


def _build_grid(center: float, config: GridConfig, regime: str = "ranging") -> list[Order]:
    """
    Build a fixed-level grid matching the live bot's asymmetric layout.

    Buy/sell split is regime-aware (trending_up skews 60/40, trending_dn
    skews 40/60, others symmetric). This mirrors grid_trader.build_grid().
    """
    spacing  = config.spacing_pct / 100
    levels   = config.levels
    buy_frac = _REGIME_GRID_SKEW.get(regime, 0.50)
    n_buys   = max(1, min(round(levels * buy_frac), levels - 1))
    n_sells  = levels - n_buys
    orders   = []

    for i in range(n_buys):
        offset = -(n_buys - i) * spacing
        price  = round(center * (1 + offset), 2)
        qty    = max(config.per_level_usdt / price, MIN_QTY_BTC)
        orders.append(Order(level=i, side="BUY", price=price, qty=round(qty, 6)))

    for j in range(n_sells):
        offset = j * spacing
        price  = round(center * (1 + offset), 2)
        qty    = max(config.per_level_usdt / price, MIN_QTY_BTC)
        orders.append(Order(level=n_buys + j, side="SELL", price=price, qty=round(qty, 6)))

    return orders


# ------------------------------------------------------------------ #
#  Fill simulation (per candle)                                        #
# ------------------------------------------------------------------ #

def _simulate_candle_fills(
    orders:    list[Order],
    candle:    dict,
    buy_queue: list,   # FIFO list of BUY fill prices (matches live bot's buy_prices_queue)
    config:    GridConfig,
    result:    BacktestResult,
) -> tuple[list[Order], list]:
    """
    Fixed-level fill simulation matching the live bot topology:
      - BUY fills   → re-place same BUY at same price; push fill price onto FIFO queue
      - SELL fills  → re-place same SELL at same price; pop oldest BUY for P&L

    Fill condition (OHLC sweep model):
      - BUY  fills if candle low  <= order price (price dipped to it)
      - SELL fills if candle high >= order price (price rose to it)
    """
    high     = candle["high"]
    low      = candle["low"]
    gbp_rate = config.gbp_usd_rate
    day_pnl  = 0.0

    for order in orders:
        if order.side == "BUY" and order.price >= low:
            fee_usdt = order.price * order.qty * MAKER_FEE
            fee_gbp  = fee_usdt / gbp_rate
            result.total_trades  += 1
            result.total_fees_gbp += fee_gbp
            buy_queue.append(order.price)
            # Fixed-level: re-place the same BUY at the same price (no mutation needed —
            # order object stays in the list unchanged for the next candle)

        elif order.side == "SELL" and order.price <= high:
            fee_usdt   = order.price * order.qty * MAKER_FEE
            fee_gbp    = fee_usdt / gbp_rate
            result.total_trades += 1
            result.total_sells  += 1

            # Pair with oldest BUY (FIFO); fall back to a zero-cost basis if queue empty
            buy_px = buy_queue.pop(0) if buy_queue else order.price
            gross_usdt = (order.price - buy_px) * order.qty
            gross_gbp  = gross_usdt / gbp_rate
            net_gbp    = gross_gbp - fee_gbp

            result.total_gross_gbp += gross_gbp
            result.total_fees_gbp  += fee_gbp
            result.total_net_gbp   += net_gbp
            day_pnl += net_gbp
            if net_gbp > 0:
                result.wins += 1

    result.daily_pnl.append(day_pnl)
    return orders, buy_queue   # orders unchanged (fixed-level)


# ------------------------------------------------------------------ #
#  Main backtest loop                                                  #
# ------------------------------------------------------------------ #

def run_backtest(
    candles: list[dict],
    config:  GridConfig,
    regime:  str = "ranging",
    verbose: bool = False,
) -> BacktestResult:
    """
    Step through each candle in order, maintaining fixed-level grid state.
    Recentres the grid when price moves > config.range_pct from calibration,
    matching the live bot's recenter trigger exactly.
    """
    if len(candles) < 5:
        print("[backtest] Not enough candles.")
        return BacktestResult()

    result      = BacktestResult()
    calibration = candles[0]["close"]
    orders      = _build_grid(calibration, config, regime)
    buy_queue: list[float] = []   # FIFO BUY fill prices

    cumulative_pnl = 0.0
    peak_pnl       = 0.0

    for candle in candles:
        price = candle["close"]

        # Recentre when price breaks out of config.range_pct band (matches live bot)
        move_pct = abs(price - calibration) / calibration * 100
        if move_pct > config.range_pct:
            calibration = price
            orders      = _build_grid(calibration, config, regime)
            buy_queue   = []   # pending BUYs lost on cancel-all, like the live bot
            result.recenter_count += 1
            if verbose:
                print(f"[backtest] Recentre at ${price:,.0f} (move={move_pct:.1f}%)")

        orders, buy_queue = _simulate_candle_fills(
            orders, candle, buy_queue, config, result
        )

        # Track max drawdown
        cumulative_pnl += result.daily_pnl[-1] if result.daily_pnl else 0
        peak_pnl        = max(peak_pnl, cumulative_pnl)
        drawdown        = peak_pnl - cumulative_pnl
        result.max_drawdown_gbp = max(result.max_drawdown_gbp, drawdown)

    return result


# ------------------------------------------------------------------ #
#  CLI entry point                                                     #
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(description="Grid strategy backtester")
    parser.add_argument("--spacing",  type=float, default=None, help="spacing_pct override")
    parser.add_argument("--levels",   type=int,   default=None, help="levels override")
    parser.add_argument("--capital",  type=float, default=None, help="capital_pct override")
    parser.add_argument("--days",     type=int,   default=30,   help="number of candles to fetch")
    parser.add_argument("--verbose",  action="store_true")
    args = parser.parse_args()

    from trading.cdx_client import CDXClient
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    config = GridConfig.from_file(ROOT / "config" / "grid_params.json")
    if args.spacing  is not None: config.spacing_pct  = args.spacing
    if args.levels   is not None: config.levels        = args.levels
    if args.capital  is not None: config.capital_pct   = args.capital

    print(f"[backtest] Fetching {args.days} daily candles...")
    cdx     = CDXClient()
    candles = cdx.get_candlesticks("BTC_USDT", timeframe="1D", count=args.days)
    print(f"[backtest] Got {len(candles)} candles. Running simulation...")
    print(f"[backtest] Config: spacing={config.spacing_pct}% levels={config.levels} "
          f"capital={config.capital_pct} kill_pct={config.kill_pct}")

    result = run_backtest(candles, config, verbose=args.verbose)

    print("\n" + "=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(result.summary())
    print("=" * 60)


if __name__ == "__main__":
    main()
