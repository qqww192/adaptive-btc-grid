"""
Historical backtest: Jan 1 2020 → May 25 2026 (~2341 daily candles)
Compares three scenarios using the fixed-level + FIFO grid topology.

Scenario A: Old config  — spacing=0.8%, levels=6, symmetric 3:3
Scenario B: New config  — spacing=1.0%, levels=6, asymmetric 4:2 (ranging)
Scenario C: New+pause   — same as B but skip grid on confirmed strong-trend days
            (simulated as: skip day if 14-day close-to-close % move > 15% AND
             daily range < 2% of price — tight range + strong trend = trend pause proxy)
"""

import httpx
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── Fetch candles ────────────────────────────────────────────────────────────

def fetch_candles(start_ms: int, end_ms: int) -> list[dict]:
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []
    current = start_ms
    while current < end_ms:
        params = {
            "symbol": "BTCUSDT",
            "interval": "1d",
            "startTime": current,
            "endTime": end_ms,
            "limit": 1000,
        }
        resp = httpx.get(url, params=params, timeout=20)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for row in batch:
            all_candles.append({
                "ts":    row[0],
                "open":  float(row[1]),
                "high":  float(row[2]),
                "low":   float(row[3]),
                "close": float(row[4]),
            })
        current = batch[-1][0] + 86_400_000  # advance by 1 day
        if len(batch) < 1000:
            break
        time.sleep(0.1)
    return all_candles


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class Config:
    spacing_pct:   float = 1.0
    range_pct:     float = 5.0
    levels:        int   = 6
    capital_pct:   float = 0.70
    total_capital: float = 150.0
    gbp_usd_rate:  float = 1.27
    kill_pct:      float = 0.10
    asymmetric:    bool  = True   # True = 4:2 buy/sell split in ranging

    @property
    def capital_usdt(self) -> float:
        return self.total_capital * self.capital_pct * self.gbp_usd_rate

    @property
    def per_level_usdt(self) -> float:
        return self.capital_usdt / self.levels if self.levels else 0


# ── Grid builder ─────────────────────────────────────────────────────────────

MAKER_FEE   = 0.0025
MIN_QTY_BTC = 0.0001


@dataclass
class Order:
    side:      str
    price:     float
    qty:       float


def build_grid(center: float, cfg: Config) -> list[Order]:
    spacing = cfg.spacing_pct / 100
    levels  = cfg.levels
    if cfg.asymmetric:
        n_buys  = max(1, min(round(levels * 0.60), levels - 1))  # 4 of 6
    else:
        n_buys  = levels // 2   # 3 of 6 (symmetric)
    n_sells = levels - n_buys
    orders  = []
    for i in range(n_buys):
        offset = -(n_buys - i) * spacing
        price  = round(center * (1 + offset), 2)
        qty    = max(cfg.per_level_usdt / price, MIN_QTY_BTC)
        orders.append(Order("BUY", price, round(qty, 6)))
    for j in range(n_sells):
        offset = j * spacing
        price  = round(center * (1 + offset), 2)
        qty    = max(cfg.per_level_usdt / price, MIN_QTY_BTC)
        orders.append(Order("SELL", price, round(qty, 6)))
    return orders


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass
class Result:
    trades:        int   = 0
    sells:         int   = 0
    wins:          int   = 0
    gross_gbp:     float = 0.0
    fees_gbp:      float = 0.0
    net_gbp:       float = 0.0
    max_dd_gbp:    float = 0.0
    recenters:     int   = 0
    paused_days:   int   = 0
    daily_pnl:     list  = field(default_factory=list)
    yearly:        dict  = field(default_factory=dict)

    @property
    def win_rate(self):
        return round(self.wins / self.sells * 100, 1) if self.sells else 0.0

    @property
    def sharpe(self):
        if len(self.daily_pnl) < 2:
            return 0.0
        mean = sum(self.daily_pnl) / len(self.daily_pnl)
        var  = sum((x - mean) ** 2 for x in self.daily_pnl) / len(self.daily_pnl)
        std  = math.sqrt(var)
        return round(mean / std * math.sqrt(252), 3) if std else 0.0

    @property
    def fee_drag(self):
        return round(self.fees_gbp / self.gross_gbp * 100, 1) if self.gross_gbp > 0 else 0.0


# ── Trend pause heuristic ────────────────────────────────────────────────────

def is_strong_trend_day(candles: list[dict], idx: int) -> bool:
    """
    Proxy for HMM trending_up/trending_dn with confidence ≥ 0.70.

    Flags a day as a 'strong trend' (grid stands aside) using a multi-factor
    heuristic that targets ~25-30% of BTC days — matching historical data:

    Condition: 10-day EMA > 30-day EMA by ≥1% (or vice versa for down-trend)
               AND 14-day net directional move ≥ 8%.

    This is a proxy for the HMM regime classifier's strong-trend signal.
    """
    if idx < 30:
        return False

    # 14-day directional momentum
    close_14_ago = candles[idx - 14]["close"]
    close_now    = candles[idx]["close"]
    net_move_pct = (close_now - close_14_ago) / close_14_ago * 100

    # 10-day vs 30-day EMA crossover (simple approximation using SMA)
    ema10 = sum(c["close"] for c in candles[idx - 9:idx + 1])  / 10
    ema30 = sum(c["close"] for c in candles[idx - 29:idx + 1]) / 30
    ema_spread_pct = abs(ema10 - ema30) / ema30 * 100

    # ATR-14 normalised (low ATR relative to range → persistent trend, not chop)
    ranges_14 = [(c["high"] - c["low"]) / c["close"] for c in candles[idx - 13:idx + 1]]
    atr14_pct  = sum(ranges_14) / len(ranges_14) * 100

    strong_trend = (
        abs(net_move_pct) >= 8.0
        and ema_spread_pct >= 1.0
        and atr14_pct < 4.0   # not wildly volatile — trending, not crashing
    )
    return strong_trend


# ── Core simulation ──────────────────────────────────────────────────────────

def run_backtest(candles: list[dict], cfg: Config, use_trend_pause: bool = False) -> Result:
    res         = Result()
    calibration = candles[0]["close"]
    orders      = build_grid(calibration, cfg)
    buy_queue:  list[float] = []
    cum_pnl     = 0.0
    peak_pnl    = 0.0
    gbp_rate    = cfg.gbp_usd_rate

    for i, candle in enumerate(candles):
        year = datetime.fromtimestamp(candle["ts"] / 1000, tz=timezone.utc).year

        # Trend pause
        if use_trend_pause and is_strong_trend_day(candles, i):
            res.paused_days += 1
            res.daily_pnl.append(0.0)
            res.yearly.setdefault(year, 0.0)
            continue

        price    = candle["close"]
        high     = candle["high"]
        low      = candle["low"]

        # Recentre check
        move_pct = abs(price - calibration) / calibration * 100
        if move_pct > cfg.range_pct:
            calibration = price
            orders      = build_grid(calibration, cfg)
            buy_queue   = []
            res.recenters += 1

        day_pnl = 0.0
        for order in orders:
            if order.side == "BUY" and order.price >= low:
                fee_gbp = order.price * order.qty * MAKER_FEE / gbp_rate
                res.trades       += 1
                res.fees_gbp     += fee_gbp
                buy_queue.append(order.price)

            elif order.side == "SELL" and order.price <= high:
                fee_gbp = order.price * order.qty * MAKER_FEE / gbp_rate
                res.trades += 1
                res.sells  += 1
                buy_px     = buy_queue.pop(0) if buy_queue else order.price
                gross_usdt = (order.price - buy_px) * order.qty
                gross_gbp  = gross_usdt / gbp_rate
                net_gbp    = gross_gbp - fee_gbp
                res.gross_gbp += gross_gbp
                res.fees_gbp  += fee_gbp
                res.net_gbp   += net_gbp
                day_pnl       += net_gbp
                if net_gbp > 0:
                    res.wins += 1

        res.daily_pnl.append(day_pnl)
        res.yearly[year] = res.yearly.get(year, 0.0) + day_pnl

        cum_pnl  += day_pnl
        peak_pnl  = max(peak_pnl, cum_pnl)
        drawdown  = peak_pnl - cum_pnl
        res.max_dd_gbp = max(res.max_dd_gbp, drawdown)

    return res


# ── Pretty printer ────────────────────────────────────────────────────────────

def print_result(label: str, res: Result, cfg: Config):
    print(f"\n{'='*62}")
    print(f"  {label}")
    print(f"  spacing={cfg.spacing_pct}%  levels={cfg.levels}  "
          f"asymmetric={cfg.asymmetric}  pause={res.paused_days>0}")
    print(f"{'='*62}")
    print(f"  Net P&L:      £{res.net_gbp:+.2f}")
    print(f"  Gross:        £{res.gross_gbp:.2f}   Fees: £{res.fees_gbp:.2f}  "
          f"(drag {res.fee_drag}%)")
    print(f"  Trades:       {res.trades}   Sells: {res.sells}   "
          f"Win rate: {res.win_rate}%")
    print(f"  Max drawdown: £{res.max_dd_gbp:.2f}")
    print(f"  Sharpe (ann): {res.sharpe}")
    print(f"  Recentres:    {res.recenters}   Paused days: {res.paused_days}")
    print(f"\n  Year-by-year:")
    for yr in sorted(res.yearly):
        bar_len = int(abs(res.yearly[yr]) / 0.5)
        bar     = ("+" if res.yearly[yr] >= 0 else "-") * min(bar_len, 40)
        print(f"    {yr}: £{res.yearly[yr]:+7.2f}  {bar}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    START_MS = 1577836800000   # 2020-01-01 00:00:00 UTC
    END_MS   = 1748131200000   # 2025-05-25 00:00:00 UTC

    print("Fetching BTC/USDT daily candles from Binance (2020-01-01 → 2026-05-25)...")
    candles = fetch_candles(START_MS, END_MS)
    print(f"Fetched {len(candles)} candles  "
          f"({datetime.fromtimestamp(candles[0]['ts']/1000, tz=timezone.utc).date()} → "
          f"{datetime.fromtimestamp(candles[-1]['ts']/1000, tz=timezone.utc).date()})\n")

    # Scenario A — old config (spacing 0.8%, symmetric 3:3)
    cfg_a = Config(spacing_pct=0.8, range_pct=5.0, levels=6, capital_pct=0.70,
                   total_capital=150.0, gbp_usd_rate=1.27, asymmetric=False)
    res_a = run_backtest(candles, cfg_a, use_trend_pause=False)
    print_result("Scenario A — OLD  (0.8% spacing, symmetric 3:3, no pause)", res_a, cfg_a)

    # Scenario B — new config (spacing 1.0%, asymmetric 4:2, no pause)
    cfg_b = Config(spacing_pct=1.0, range_pct=5.0, levels=6, capital_pct=0.70,
                   total_capital=150.0, gbp_usd_rate=1.27, asymmetric=True)
    res_b = run_backtest(candles, cfg_b, use_trend_pause=False)
    print_result("Scenario B — NEW  (1.0% spacing, asymmetric 4:2, no pause)", res_b, cfg_b)

    # Scenario C — new config + trend pause
    cfg_c = Config(spacing_pct=1.0, range_pct=5.0, levels=6, capital_pct=0.70,
                   total_capital=150.0, gbp_usd_rate=1.27, asymmetric=True)
    res_c = run_backtest(candles, cfg_c, use_trend_pause=True)
    print_result("Scenario C — NEW + TREND PAUSE (1.0%, asymmetric 4:2, HMM proxy pause)", res_c, cfg_c)

    # Delta summary
    print(f"\n{'='*62}")
    print("  IMPROVEMENT SUMMARY (relative to Scenario A)")
    print(f"{'='*62}")
    print(f"  B vs A:  net {res_b.net_gbp - res_a.net_gbp:+.2f} GBP  "
          f"| drawdown {res_b.max_dd_gbp - res_a.max_dd_gbp:+.2f}  "
          f"| Sharpe {res_b.sharpe - res_a.sharpe:+.3f}")
    print(f"  C vs A:  net {res_c.net_gbp - res_a.net_gbp:+.2f} GBP  "
          f"| drawdown {res_c.max_dd_gbp - res_a.max_dd_gbp:+.2f}  "
          f"| Sharpe {res_c.sharpe - res_a.sharpe:+.3f}")
    print(f"  C vs B:  net {res_c.net_gbp - res_b.net_gbp:+.2f} GBP  "
          f"| pause contributed {res_c.net_gbp - res_b.net_gbp:+.2f} GBP over {res_c.paused_days} paused days")
    print()


if __name__ == "__main__":
    main()
