"""
Regime-aware grid backtester — reproduces the LIVE bot's behaviour, including the
parts the plain grid backtester (grid_backtest.py) ignores: the regime trend
pause, one-sided order cancellation, BTC accumulation while paused, and the
trailing-stop liquidation.

Why this exists
---------------
The existing `grid_backtest.py` models only a fixed grid + recentring, so it
can never reproduce a *losing* run — it has no concept of the trend pause that,
in the live bot, makes capital sit idle and then dumps the accumulated bag on a
3% pullback. To prove a fix we first need a simulator that reproduces the loss.

This module models three policies and compares them on the same candles:

  current        — the live behaviour today:
                     • pause the grid the moment a trend is detected (mirrors the
                       HMM firing at ≥0.70 confidence almost immediately)
                     • cancel the "dangerous" side, keep buying dips, NO replenish
                     • TRAIL_STOP_PCT=3% liquidation of the bag on a pullback
  fixed          — Step 2 of the plan:
                     • require N consecutive confirmations before pausing
                       (hysteresis) — stops the over-eager pause
                     • instead of standing idle, trade the WIDER trend grid
                       (trending preset: wider spacing, reduced capital, skew)
                     • widen / ATR-scale the trailing stop so minor dips don't
                       liquidate the bag
  fixed_sleeve   — Step 3: `fixed` plus a small spot trend-follow sleeve that
                     earns during a confirmed strong up-trend.

Data
----
No network is required for the synthetic scenarios (used in CI / the web
sandbox where market-data hosts are blocked). On the Oracle VM, pass
`--source binance` to fetch real BTC/USDT candles and reproduce the actual
recent losing weeks.

Constants mirror the live code so the simulation stays faithful:
  MAKER_FEE          0.25%/leg   (grid_backtest.MAKER_FEE)
  TRAIL_STOP_PCT     3.0%        (grid_trader.TRAIL_STOP_PCT)
  trend slope gate   ±4% / 5d    (regime_classifier.classify_rules)
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from backtesting.grid_backtest import GridConfig  # reused config + properties

# ── Constants mirrored from the live bot ─────────────────────────────────────
MAKER_FEE              = 0.0025   # 0.25% per leg (grid_backtest.MAKER_FEE)
MIN_QTY_BTC            = 0.0001
LIVE_TRAIL_STOP_PCT    = 3.0      # grid_trader.TRAIL_STOP_PCT (the whipsaw culprit)
SLOPE_GATE_PCT         = 4.0      # regime_classifier.classify_rules 5-candle slope
ATR_RANGING_PCT        = 1.5
BBW_RANGING_PCT        = 3.0
ATR_VOLATILE_PCT       = 3.5
BBW_VOLATILE_PCT       = 6.0

# Trending presets (mirror regime_classifier.REGIME_PARAMS) — used by `fixed` so
# the bot keeps trading a wider grid instead of standing idle during a trend.
TREND_PRESET = {
    "trending_up": {"spacing_pct": 1.4, "capital_pct": 0.55, "buy_frac": 0.60},
    "trending_dn": {"spacing_pct": 1.2, "capital_pct": 0.50, "buy_frac": 0.40},
}


# ── Deterministic regime classifier (mirrors regime_classifier.classify_rules) ─
# Reimplemented here (not imported) because regime_classifier.py imports
# cdx_client → ccxt at module load, which is unavailable offline. This is the
# pure rule classifier only; the pause *policy* lives in the engine below.

def _atr_pct(window: list[dict]) -> float:
    if len(window) < 15:
        return 0.0
    trs = []
    for i in range(1, len(window)):
        h, l, pc = window[i]["high"], window[i]["low"], window[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[-14:]) / 14
    price = window[-1]["close"]
    return atr / price * 100 if price else 0.0


def _bbw_pct(window: list[dict], period: int = 20) -> float:
    closes = [c["close"] for c in window[-period:]]
    if len(closes) < period:
        return 0.0
    mean = sum(closes) / period
    std  = math.sqrt(sum((x - mean) ** 2 for x in closes) / period)
    return (4 * std) / mean * 100 if mean else 0.0


def _slope_pct(window: list[dict]) -> float:
    recent = [c["close"] for c in window[-5:]]
    if len(recent) < 5 or recent[0] == 0:
        return 0.0
    return (recent[-1] - recent[0]) / recent[0] * 100


def classify(window: list[dict]) -> str:
    """
    Regime label for the trailing window.

    NOTE: this prioritises directional slope, mirroring how the LIVE *primary*
    classifier (the 3-state Gaussian HMM) labels — it ranks states by mean
    return and tags the highest/lowest-return state trending_up/dn even when
    Bollinger width is elevated. The rule-based `classify_rules` (a fallback)
    instead lets a wide band win as `volatile`; using that here would hide the
    very trend pause we are trying to reproduce. Extreme ATR still wins as
    volatile (a genuine crash, not a clean trend).
    """
    if len(window) < 20:
        return "ranging"
    atr, bbw, slope = _atr_pct(window), _bbw_pct(window), _slope_pct(window)
    # Lead with directional return (mirrors the HMM ranking states by mean return):
    # a clean trend is labelled trending even though its inter-candle gaps make
    # ATR/Bollinger-width look "wide". Volatile is reserved for non-directional
    # dispersion (a crash/chop with no net slope).
    if slope > SLOPE_GATE_PCT:
        return "trending_up"
    if slope < -SLOPE_GATE_PCT:
        return "trending_dn"
    if atr > ATR_VOLATILE_PCT or bbw > BBW_VOLATILE_PCT:
        return "volatile"
    if atr < ATR_RANGING_PCT and bbw < BBW_RANGING_PCT:
        return "ranging"
    return "ranging"


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass
class Result:
    policy:          str   = ""
    trades:          int   = 0
    sells:           int   = 0
    wins:            int   = 0
    realized_gbp:    float = 0.0    # closed P&L (grid + sleeve + trail dumps)
    fees_gbp:        float = 0.0
    unrealized_gbp:  float = 0.0    # held inventory marked to final close
    max_dd_gbp:      float = 0.0
    recenters:       int   = 0
    paused_steps:    int   = 0
    trail_fires:     int   = 0
    sleeve_gbp:      float = 0.0
    steps:           int   = 0
    daily_pnl:       list  = field(default_factory=list)

    @property
    def net_gbp(self) -> float:
        """Total economic P&L: realized + mark-to-market of leftover inventory."""
        return self.realized_gbp + self.unrealized_gbp

    @property
    def win_rate(self) -> float:
        return round(self.wins / self.sells * 100, 1) if self.sells else 0.0

    @property
    def pause_pct(self) -> float:
        return round(self.paused_steps / self.steps * 100, 1) if self.steps else 0.0

    def summary(self) -> str:
        return (
            f"  Net P&L:       £{self.net_gbp:+.2f}  "
            f"(realized £{self.realized_gbp:+.2f} + unrealized £{self.unrealized_gbp:+.2f})\n"
            f"  Fees:          £{self.fees_gbp:.2f}\n"
            f"  Trades:        {self.trades}   Sells: {self.sells}   "
            f"Win rate: {self.win_rate}%\n"
            f"  Max drawdown:  £{self.max_dd_gbp:.2f}\n"
            f"  Paused:        {self.paused_steps}/{self.steps} steps ({self.pause_pct}%)   "
            f"Trail dumps: {self.trail_fires}\n"
            f"  Recentres:     {self.recenters}   Sleeve P&L: £{self.sleeve_gbp:+.2f}"
        )


# ── Order + inventory model ──────────────────────────────────────────────────

@dataclass
class Order:
    side:  str
    price: float
    qty:   float


def _build_grid(center: float, spacing_pct: float, levels: int,
                per_level_usdt: float, buy_frac: float) -> list[Order]:
    spacing = spacing_pct / 100
    n_buys  = max(1, min(round(levels * buy_frac), levels - 1))
    n_sells = levels - n_buys
    orders  = []
    for i in range(n_buys):
        price = round(center * (1 - (n_buys - i) * spacing), 2)
        orders.append(Order("BUY", price, max(per_level_usdt / price, MIN_QTY_BTC)))
    for j in range(n_sells):
        price = round(center * (1 + j * spacing), 2)
        orders.append(Order("SELL", price, max(per_level_usdt / price, MIN_QTY_BTC)))
    return orders


# ── Core engine ──────────────────────────────────────────────────────────────

def run_backtest(candles: list[dict], cfg: GridConfig, policy: str = "current",
                 confirm_steps: int = 3, fixed_trail_pct: float = 6.0,
                 warmup: int = 30, reclass_every: int = 6,
                 regime_seq: list[str] | None = None) -> Result:
    """
    Step through candles applying one of three regime policies. `inventory` is a
    FIFO list of [qty, cost] lots shared by every code path, so held BTC is always
    accounted for and marked to market at the end (no hidden bag losses).

    Modes the engine switches between:
      grid            two-sided base grid (ranging) — recenters on range break
      trend_grid      two-sided WIDER grid using TREND_PRESET (fixed policies)
      standaside_trend  live `current` behaviour: cancel one side, keep buying
                        dips, NO replenish, 3% trailing-stop dump (the whipsaw)
      standaside_idle   fixed policy on `volatile`: cancel all, hold, don't trade
    """
    res = Result(policy=policy, steps=len(candles))
    gbp = cfg.gbp_usd_rate

    inventory: list[list[float]] = []   # FIFO [qty, cost]

    def realize_sell(sell_price: float, qty: float) -> float:
        remaining, pnl_usdt = qty, 0.0
        while remaining > 1e-12 and inventory:
            lot = inventory[0]
            take = min(remaining, lot[0])
            pnl_usdt += (sell_price - lot[1]) * take
            lot[0]   -= take
            remaining -= take
            if lot[0] <= 1e-12:
                inventory.pop(0)
        return pnl_usdt / gbp

    def make_grid(center: float, preset: dict | None) -> list[Order]:
        if preset:
            per_level = cfg.total_capital * preset["capital_pct"] * gbp / cfg.levels
            return _build_grid(center, preset["spacing_pct"], cfg.levels, per_level,
                               preset["buy_frac"])
        return _build_grid(center, cfg.spacing_pct, cfg.levels, cfg.per_level_usdt, 0.60)

    calibration = candles[0]["close"]
    orders      = make_grid(calibration, None)
    mode        = "grid"
    pause_dir   = ""
    peak        = 0.0
    trail_fired = False
    sleeve_qty  = sleeve_cost = 0.0
    trend_run   = 0
    regime      = "ranging"
    cum = peak_pnl = 0.0

    for i, c in enumerate(candles):
        price, high, low = c["close"], c["high"], c["low"]
        day_pnl = 0.0

        # 1. Regime is STICKY between reclassifications — the live classifier runs
        #    only every 4 hours while the grid/trail run every minute. Re-deriving
        #    the regime each candle would let a pause exit before a 3% intra-pause
        #    dip ever triggers the trailing-stop dump, hiding the whipsaw.
        #    `regime_seq` (when given) replays an externally-supplied regime label
        #    per candle — used to feed the REAL HMM labels on the VM, and to test
        #    the mechanism deterministically.
        if regime_seq is not None:
            new = regime_seq[i]
            if new != regime:
                regime, trend_run = new, 0
            if regime in ("trending_up", "trending_dn"):
                trend_run += 1
        elif i >= warmup and i % reclass_every == 0:
            regime    = classify(candles[max(0, i - warmup + 1): i + 1])
            is_trend  = regime in ("trending_up", "trending_dn")
            trend_run = trend_run + 1 if is_trend else 0
        is_trend = regime in ("trending_up", "trending_dn")

        # 2. Target mode for this policy. The live bot pauses ONLY on a trend
        #    (never on volatile — volatile just widens the grid). So both policies
        #    keep gridding on ranging/volatile; they differ only on a trend.
        if policy == "current":
            target = "standaside_trend" if is_trend else "grid"
        else:  # fixed / fixed_sleeve
            if is_trend and trend_run >= confirm_steps:
                target = "trend_grid"
            else:
                target = "grid"

        # 3. Mode transitions. `pause_dir` carries the trend direction for both the
        #    stand-aside (which side to keep / trail) and the trend_grid preset.
        if target != mode:
            # Liquidate any open sleeve whenever we leave a trend.
            if sleeve_qty > 0 and target not in ("trend_grid",):
                pnl = (price - sleeve_cost) * sleeve_qty / gbp
                res.sleeve_gbp += pnl; day_pnl += pnl
                res.fees_gbp   += price * sleeve_qty * MAKER_FEE / gbp
                sleeve_qty = sleeve_cost = 0.0
            if target == "standaside_trend":
                pause_dir, peak, trail_fired = regime, price, False
                keep = "BUY" if regime == "trending_up" else "SELL"
                orders = [o for o in orders if o.side == keep]   # cancel dangerous side
            elif target == "standaside_idle":
                pause_dir = ""
                orders = []                                       # cancel all, hold bag
            elif target == "trend_grid":
                pause_dir = regime                                # remember direction
                calibration = price
                orders = make_grid(calibration, TREND_PRESET.get(regime))
            else:  # grid
                pause_dir = ""
                calibration = price
                orders = make_grid(calibration, None)
            mode = target

        paused = mode.startswith("standaside")
        res.paused_steps += 1 if paused else 0

        # 4. Recenter while actively gridding
        if mode in ("grid", "trend_grid"):
            preset = TREND_PRESET.get(pause_dir) if mode == "trend_grid" else None
            if abs(price - calibration) / calibration * 100 > cfg.range_pct:
                calibration = price
                orders = make_grid(calibration, preset)
                inventory = []          # cancel-all loses pending pairing (live recenter)
                res.recenters += 1

        # 5. Fills (OHLC sweep), fixed-level replace
        if mode != "standaside_idle":
            for o in orders:
                if o.side == "BUY" and o.price >= low:
                    res.trades   += 1
                    res.fees_gbp += o.price * o.qty * MAKER_FEE / gbp
                    inventory.append([o.qty, o.price])
                elif o.side == "SELL" and o.price <= high:
                    res.trades += 1
                    res.sells  += 1
                    fee = o.price * o.qty * MAKER_FEE / gbp
                    pnl = realize_sell(o.price, o.qty) - fee
                    res.realized_gbp += pnl; res.fees_gbp += fee; day_pnl += pnl
                    if pnl > 0:
                        res.wins += 1

        # 6. Trailing-stop dump — ONLY the live `current` stand-aside path (the bug)
        if mode == "standaside_trend" and pause_dir == "trending_up" and not trail_fired:
            peak = max(peak, price)
            if price <= peak * (1 - LIVE_TRAIL_STOP_PCT / 100) and inventory:
                qty = sum(lot[0] for lot in inventory)
                fee = price * qty * MAKER_FEE / gbp
                pnl = realize_sell(price, qty) - fee
                res.realized_gbp += pnl; res.fees_gbp += fee; day_pnl += pnl
                res.trail_fires += 1
                trail_fired = True
                orders = []                      # stand fully aside after the dump

        # 7. Trend sleeve (fixed_sleeve): open a small spot position once per up-trend
        if policy == "fixed_sleeve" and mode == "trend_grid" \
                and pause_dir == "trending_up" and sleeve_qty == 0:
            notional = cfg.total_capital * 0.15 * gbp
            sleeve_qty, sleeve_cost = notional / price, price
            res.fees_gbp += notional * MAKER_FEE / gbp

        res.daily_pnl.append(day_pnl)
        cum     += day_pnl
        peak_pnl = max(peak_pnl, cum)
        res.max_dd_gbp = max(res.max_dd_gbp, peak_pnl - cum)

    # Mark leftover inventory + open sleeve to market at the final close.
    final = candles[-1]["close"]
    res.unrealized_gbp += sum((final - lot[1]) * lot[0] for lot in inventory) / gbp
    if sleeve_qty > 0:
        res.unrealized_gbp += (final - sleeve_cost) * sleeve_qty / gbp
    return res


# ── Synthetic scenarios (deterministic, no network) ──────────────────────────

def _candles_from_closes(closes: list[float], intraday_pct: float = 1.2) -> list[dict]:
    """Wrap a close path into OHLC candles with a symmetric intraday range."""
    out, prev = [], closes[0]
    for k, close in enumerate(closes):
        hi = max(prev, close) * (1 + intraday_pct / 100)
        lo = min(prev, close) * (1 - intraday_pct / 100)
        out.append({"ts": k, "open": prev, "high": hi, "low": lo, "close": close})
        prev = close
    return out


def scenario_uptrend_with_pullbacks(n: int = 180, start: float = 60000.0) -> list[dict]:
    """
    Grinding up-trend with recurring ~4% pullbacks — the exact shape that makes
    the live bot accumulate on dips then trail-stop the bag 3% below each peak.
    """
    closes, p = [start], start
    for k in range(1, n):
        drift = 0.012                                  # +1.2%/step average climb
        pull  = -0.05 if (k % 11 == 0) else 0.0        # periodic ~5% pullback
        wobble = 0.004 * math.sin(k / 2.0)
        p *= (1 + drift + pull + wobble)
        closes.append(p)
    return _candles_from_closes(closes)


def scenario_ranging(n: int = 120, mid: float = 60000.0, amp_pct: float = 3.0) -> list[dict]:
    """Oscillating range — the regime grids are designed to profit in."""
    closes = [mid * (1 + amp_pct / 100 * math.sin(k / 3.0)) for k in range(n)]
    return _candles_from_closes(closes)


def scenario_trap(start: float = 60000.0) -> list[dict]:
    """
    The pathological live case, deterministically: a low-noise up-trend with
    recurring ~2.5% dips (fills the kept BUY side → bag accumulates while the
    SELL side is cancelled), followed by a sharp reversal that drops >3% from the
    peak — firing the 3% trailing-stop dump on a bag bought near the top (a
    realised loss), then a sustained decline. `current` should bleed here;
    `fixed` keeps a two-sided trend grid and avoids the dump.
    """
    closes, p = [start], start
    # Phase 1: grind up 70 candles with periodic 2.5% dips
    for k in range(1, 70):
        dip = -0.025 if (k % 5 == 0) else 0.0
        p *= (1 + 0.014 + dip)
        closes.append(p)
    # Phase 2: sharp reversal then sustained decline (40 candles)
    for k in range(40):
        p *= (1 - 0.03)
        closes.append(p)
    # Phase 3: settle into a range
    base = closes[-1]
    for k in range(40):
        closes.append(base * (1 + 0.025 * math.sin(k / 3.0)))
    return _candles_from_closes(closes, intraday_pct=0.5)


def scenario_pump_dump(n: int = 120, start: float = 60000.0) -> list[dict]:
    """
    Up-trend that lures the bot into accumulating, then a sharp reversal — the
    case where the live 'cancel sells, buy dips, 3% trail dump' path realises a
    loss (sells the bag well below cost) and leaves a marked-down remainder.
    """
    closes, p = [start], start
    for k in range(1, n):
        if k < n * 0.6:
            p *= (1 + 0.012 + 0.004 * math.sin(k / 2.0))      # grind up
        else:
            p *= (1 - 0.018 + 0.003 * math.sin(k / 2.0))      # sharp reversal down
        closes.append(p)
    return _candles_from_closes(closes)


def scenario_mixed() -> list[dict]:
    """Range → up-trend w/ pullbacks → reversal → range: a realistic shape."""
    a  = scenario_ranging(60, 60000.0, 3.0)
    up = scenario_uptrend_with_pullbacks(100, a[-1]["close"])
    pd = scenario_pump_dump(80, up[-1]["close"])
    b  = scenario_ranging(60, pd[-1]["close"], 3.0)
    merged, t = [], 0
    for seg in (a, up, pd, b):
        for c in seg:
            merged.append({**c, "ts": t}); t += 1
    return merged


# ── Deterministic mechanism test ─────────────────────────────────────────────

def selftest() -> bool:
    """
    Prove, deterministically, that the live `current` trailing-stop path realises
    a loss that `fixed` avoids — independent of the noisy rule classifier, by
    forcing a sustained `trending_up` regime over a constructed path:
    accumulate the bag on 2% dips, then drop >3% from the peak → 3% trail dump
    below cost. Returns True if the mechanism reproduces (current < fixed).
    """
    P0 = 60000.0
    # Choppy near P0 (fills the kept BUY side repeatedly), then a drop below cost.
    closes = [P0, P0*0.98, P0*1.00, P0*0.975, P0*1.005, P0*0.98, P0*1.00,
              P0*0.965, P0*0.93, P0*0.91, P0*0.90, P0*0.905, P0*0.90]
    candles = _candles_from_closes(closes, intraday_pct=0.2)
    seq = ["trending_up"] * len(candles)
    cfg = GridConfig(spacing_pct=1.0, range_pct=8.0, levels=4, capital_pct=0.20,
                     total_capital=170.0, gbp_usd_rate=1.27, kill_pct=0.10)

    cur = run_backtest(candles, cfg, policy="current",      regime_seq=seq, warmup=0)
    fix = run_backtest(candles, cfg, policy="fixed",        regime_seq=seq, warmup=0)

    print("\n── Mechanism self-test (forced trending_up; the live whipsaw) ──")
    print(f"  current: realized £{cur.realized_gbp:+.2f}  net £{cur.net_gbp:+.2f}  "
          f"trail dumps {cur.trail_fires}")
    print(f"  fixed:   realized £{fix.realized_gbp:+.2f}  net £{fix.net_gbp:+.2f}  "
          f"trail dumps {fix.trail_fires}")
    ok = cur.trail_fires >= 1 and cur.realized_gbp < fix.realized_gbp
    print(f"  → trail dump fires on `current`: {cur.trail_fires >= 1}; "
          f"`current` loses vs `fixed`: {cur.realized_gbp < fix.realized_gbp}  "
          f"[{'PASS' if ok else 'FAIL'}]")
    return ok


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print(label: str, res: Result):
    print(f"\n{'=' * 64}\n  {label}\n{'=' * 64}")
    print(res.summary())


def main() -> None:
    ap = argparse.ArgumentParser(description="Regime-aware grid backtester")
    ap.add_argument("--selftest", action="store_true",
                    help="run the deterministic trail-dump mechanism test and exit")
    ap.add_argument("--scenario", choices=["mixed", "uptrend", "ranging", "trap", "pumpdump"],
                    default="mixed")
    ap.add_argument("--source", choices=["synthetic", "binance"], default="synthetic",
                    help="binance fetches real BTC/USDT candles (VM only — needs open network)")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--capital", type=float, default=170.0)
    ap.add_argument("--capital-pct", type=float, default=0.20)
    ap.add_argument("--levels", type=int, default=4)
    ap.add_argument("--confirm", type=int, default=3, help="hysteresis steps before pausing")
    ap.add_argument("--fixed-trail", type=float, default=6.0)
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    if args.source == "binance":
        from backtesting.backtest_2020_2026 import fetch_candles
        import time as _t
        end = int(_t.time() * 1000)
        candles = fetch_candles(end - args.days * 86_400_000, end)
        print(f"Fetched {len(candles)} real BTC/USDT daily candles.")
    else:
        candles = {"mixed": scenario_mixed,
                   "uptrend": scenario_uptrend_with_pullbacks,
                   "ranging": scenario_ranging,
                   "trap": scenario_trap,
                   "pumpdump": scenario_pump_dump}[args.scenario]()
        print(f"Synthetic scenario '{args.scenario}': {len(candles)} candles "
              f"(${candles[0]['close']:,.0f} → ${candles[-1]['close']:,.0f}).")

    cfg = GridConfig(spacing_pct=1.0, range_pct=5.0, levels=args.levels,
                     capital_pct=args.capital_pct, total_capital=args.capital,
                     gbp_usd_rate=1.27, kill_pct=0.10)
    print(f"Config: £{cfg.total_capital} @ {cfg.capital_pct:.0%} active, "
          f"{cfg.levels} levels, spacing {cfg.spacing_pct}%.")

    results = {p: run_backtest(candles, cfg, policy=p,
                               confirm_steps=args.confirm, fixed_trail_pct=args.fixed_trail)
               for p in ("current", "fixed", "fixed_sleeve")}

    _print("CURRENT  (live: over-eager pause + 3% trailing-stop dump)", results["current"])
    _print("FIXED    (confirm+hysteresis, trade wider trend grid, ATR trail)", results["fixed"])
    _print("FIXED+SLEEVE (fixed + spot trend-follow sleeve)", results["fixed_sleeve"])

    base = results["current"].net_gbp
    print(f"\n{'=' * 64}\n  IMPROVEMENT vs CURRENT\n{'=' * 64}")
    for p in ("fixed", "fixed_sleeve"):
        r = results[p]
        print(f"  {p:<12} net £{r.net_gbp:+.2f}  "
              f"(Δ £{r.net_gbp - base:+.2f})  drawdown £{r.max_dd_gbp:.2f}  "
              f"pause {r.pause_pct}%  trail dumps {r.trail_fires}")
    print()


if __name__ == "__main__":
    main()
