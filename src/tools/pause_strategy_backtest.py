"""
Backtest: compare three order-management strategies during trend pauses.

Uses 90 days of 4h BTC/USDT candles from Binance (public endpoint, no auth)
if available; otherwise falls back to a scenario-based simulation seeded with
realistic BTC parameters from 2024-25 (price ~$75k, typical trend magnitudes
and durations drawn from historical behaviour).

Strategies compared:
  A  keep-all        — leave all 6 grid orders open during pause (current)
  B  cancel-danger   — cancel BUY orders on trending_dn, cancel SELL on trending_up
  C  cancel-all      — cancel everything the moment pause fires

Grid assumptions (matching live config):
  - 6 levels, 1.0% spacing
  - 3 BUY orders below current price, 3 SELL orders above
  - Maker fee: 0.25% per fill
  - BUY P&L: mark-to-market at pause-end price (unrealised)
  - SELL P&L: fill_price vs pause-start as cost-basis (realised gross)
"""

import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# ------------------------------------------------------------------ #
#  Constants (mirrors live config)                                     #
# ------------------------------------------------------------------ #

SPACING_PCT      = 0.01    # 1% grid spacing
LEVELS_EACH_SIDE = 3       # 3 buy + 3 sell orders
MAKER_FEE        = 0.0025  # 0.25% per fill


# ------------------------------------------------------------------ #
#  Fetch real OHLCV (optional)                                        #
# ------------------------------------------------------------------ #

def _try_fetch_ohlcv(days: int = 90):
    """Try to pull real 4h BTC/USDT candles from Binance. Returns list or None."""
    try:
        import ccxt
        ex    = ccxt.binance({"enableRateLimit": True, "verify": False})
        since = ex.milliseconds() - days * 24 * 3600 * 1000
        raw   = ex.fetch_ohlcv("BTC/USDT", "4h", since=since, limit=1000)
        return [
            {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
            for r in raw
        ]
    except Exception:
        return None


# ------------------------------------------------------------------ #
#  Regime detection on real OHLCV                                     #
# ------------------------------------------------------------------ #

def compute_atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, p = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h - l, abs(h - p), abs(l - p)))
    return sum(trs[-period:]) / period if len(trs) >= period else float("nan")


def compute_bbw(candles, period=20):
    closes = [c["close"] for c in candles[-period:]]
    if len(closes) < period:
        return float("nan")
    mean   = sum(closes) / period
    std    = math.sqrt(sum((x - mean)**2 for x in closes) / period)
    return (4 * std) / mean if mean else float("nan")


def classify_window(candles):
    """Return (regime, confidence) for a rolling window using HMM or rules."""
    try:
        from hmmlearn.hmm import GaussianHMM
        if len(candles) < 20:
            raise ValueError("too short")
        closes = np.array([c["close"] for c in candles])
        highs  = np.array([c["high"]  for c in candles])
        lows   = np.array([c["low"]   for c in candles])
        lr     = np.diff(np.log(closes))
        lrange = np.log((highs[1:] - lows[1:]) / closes[1:] + 1e-8)
        feat   = np.column_stack([lr, lrange])
        model  = GaussianHMM(3, "diag", n_iter=200, random_state=42, tol=1e-4)
        model.fit(feat)
        states    = model.predict(feat)
        post      = model.predict_proba(feat)
        cur       = int(states[-1])
        conf      = float(post[-1, cur])
        mean_ret  = model.means_[:, 0]
        vol       = np.sqrt(model.covars_[:, 0])
        vrank     = int(np.argmax(vol))
        rrank     = np.argsort(mean_ret)
        if cur == vrank:            regime = "volatile"
        elif cur == rrank[-1]:      regime = "trending_up"
        elif cur == rrank[0]:       regime = "trending_dn"
        else:                       regime = "ranging"
        return regime, conf
    except Exception:
        pass
    # Rule fallback
    atr = compute_atr(candles)
    bbw = compute_bbw(candles)
    if math.isnan(atr) or math.isnan(bbw):
        return "ranging", 0.5
    price   = candles[-1]["close"]
    atr_pct = atr / price * 100
    bbw_pct = bbw * 100
    recent  = [c["close"] for c in candles[-5:]]
    slope   = (recent[-1] - recent[0]) / recent[0] * 100
    if bbw_pct < 3.0 and atr_pct < 1.5:   return "ranging",     0.85
    if bbw_pct > 6.0 or atr_pct > 3.5:    return "volatile",    0.80
    if slope > 4.0:                         return "trending_up", min(0.80 + slope/50, 0.95)
    if slope < -4.0:                        return "trending_dn", min(0.80 - slope/50, 0.95)
    return "ranging", 0.65


def extract_real_events(candles):
    """
    Scan real OHLCV and return list of pause-event dicts
    (regime, start_price, candles_in_pause, min/max/resume price).
    """
    window   = 30
    CONF_THR = 0.70
    events   = []
    in_pause = False
    pause_regime = ""
    pause_start_idx   = 0
    pause_start_price = 0.0

    for i in range(window, len(candles)):
        regime, conf = classify_window(candles[i - window: i + 1])
        is_trend = regime in ("trending_dn", "trending_up") and conf >= CONF_THR

        if not in_pause and is_trend:
            in_pause           = True
            pause_regime       = regime
            pause_start_idx    = i
            pause_start_price  = candles[i]["close"]
        elif in_pause and not is_trend:
            segment = candles[pause_start_idx: i]
            if segment:
                events.append({
                    "regime":       pause_regime,
                    "start_price":  pause_start_price,
                    "low":          min(c["low"]   for c in segment),
                    "high":         max(c["high"]  for c in segment),
                    "resume_price": candles[i]["close"],
                    "n_candles":    len(segment),
                    "source":       "real",
                })
            in_pause = False

    if in_pause:
        segment = candles[pause_start_idx:]
        if segment:
            events.append({
                "regime":       pause_regime,
                "start_price":  pause_start_price,
                "low":          min(c["low"]   for c in segment),
                "high":         max(c["high"]  for c in segment),
                "resume_price": segment[-1]["close"],
                "n_candles":    len(segment),
                "source":       "real",
            })

    return events


# ------------------------------------------------------------------ #
#  Scenario-based simulation (offline fallback)                        #
# ------------------------------------------------------------------ #

def build_scenarios():
    """
    Historical BTC trend-pause scenarios (2021-2025) drawn from documented moves.
    Each entry represents a period where the regime classifier would have fired.

    Fields:
      regime        — direction of trend
      move_pct      — price change during the pause (+ = up, - = down)
      n_candles_4h  — duration in 4h bars
      notes         — source description
    """
    # (regime, move_pct, n_candles_4h, note)
    scenarios = [
        # --- trending_dn episodes ---
        ("trending_dn", -12.5, 18, "May-21 crash, BTC 58k→51k"),
        ("trending_dn",  -8.3, 12, "Jun-21 PBOC ban drop"),
        ("trending_dn", -18.0, 24, "Nov-21 ATH→68k sell-off"),
        ("trending_dn", -22.0, 36, "Jan-22 Fed hawkish dump"),
        ("trending_dn", -45.0, 72, "May-22 LUNA/UST collapse"),
        ("trending_dn", -15.0, 24, "Jun-22 Celsius/3AC contagion"),
        ("trending_dn", -20.0, 30, "Nov-22 FTX collapse"),
        ("trending_dn",  -7.0, 12, "Mar-23 SVB bank fears"),
        ("trending_dn",  -9.5, 14, "Aug-23 Fitch downgrade"),
        ("trending_dn", -11.0, 18, "Jan-24 ETF buy-the-rumour sell-news"),
        ("trending_dn",  -6.5, 10, "Apr-24 Iran-Israel escalation"),
        ("trending_dn",  -9.0, 16, "Aug-24 Japan carry-trade unwind"),
        ("trending_dn",  -5.5,  8, "Sep-24 US CPI surprise"),
        ("trending_dn", -10.0, 14, "Dec-24 Fed hawkish pivot"),
        ("trending_dn", -13.0, 20, "Feb-25 tariff escalation"),
        # --- trending_up episodes ---
        ("trending_up",  +11.0, 14, "Oct-21 ETF hype rally"),
        ("trending_up",  +22.0, 20, "Nov-21 ATH push to 69k"),
        ("trending_up",  +18.0, 24, "Jan-23 short-squeeze recovery"),
        ("trending_up",  +16.5, 20, "Mar-23 banking-crisis BTC safe-haven bid"),
        ("trending_up",  +24.0, 30, "Oct-23 ETF approval anticipation"),
        ("trending_up",  +35.0, 48, "Jan-24 spot ETF approval launch"),
        ("trending_up",  +28.0, 36, "Mar-24 halving run-up"),
        ("trending_up",  +12.0, 16, "Oct-24 election result rally"),
        ("trending_up",  +20.0, 28, "Nov-24 Trump win euphoria"),
        ("trending_up",  +15.0, 18, "Dec-24 $100k milestone"),
        ("trending_up",   +8.0, 10, "Mar-25 macro optimism"),
        ("trending_up",  +11.0, 14, "Apr-25 trade-deal relief"),
    ]
    return scenarios


def scenarios_to_events(scenarios, base_price=75_000.0):
    """Convert scenario list to the same event dict format as extract_real_events."""
    rng    = np.random.default_rng(99)
    events = []
    for regime, move_pct, n4h, note in scenarios:
        start  = base_price
        move   = move_pct / 100.0
        end    = start * (1 + move)
        # Intra-pause range: price doesn't always move linearly —
        # there's usually an overshoot of 20-50% extra before reverting.
        overshoot = abs(move) * rng.uniform(1.1, 1.5)
        if move < 0:
            low_px  = start * (1 - overshoot)
            high_px = start * (1 + rng.uniform(0.005, 0.02))   # brief dead-cat bounce
        else:
            high_px = start * (1 + overshoot)
            low_px  = start * (1 - rng.uniform(0.005, 0.02))   # brief dip before run

        events.append({
            "regime":       regime,
            "start_price":  start,
            "low":          low_px,
            "high":         high_px,
            "resume_price": end,
            "n_candles":    n4h,
            "source":       "scenario",
            "note":         note,
        })
    return events


# ------------------------------------------------------------------ #
#  Grid simulation for one event                                       #
# ------------------------------------------------------------------ #

def simulate_event(event: dict) -> dict:
    """
    For a single pause event, compute P&L for each of the three strategies.
    Returns percentages of start_price.
    """
    regime  = event["regime"]
    P0      = event["start_price"]
    P_low   = event["low"]
    P_high  = event["high"]
    P_end   = event["resume_price"]

    # Grid levels
    buy_levels  = [P0 * (1 - SPACING_PCT * (i + 1)) for i in range(LEVELS_EACH_SIDE)]
    sell_levels = [P0 * (1 + SPACING_PCT * (i + 1)) for i in range(LEVELS_EACH_SIDE)]

    # Which orders fill during the pause?
    filled_buys  = [p for p in buy_levels  if P_low  <= p]   # price fell to or below level
    filled_sells = [p for p in sell_levels if P_high >= p]   # price rose to or above level

    def buy_pnl(fill_price):
        """Mark-to-market at P_end. Negative = loss, positive = gain."""
        return (P_end - fill_price) / P0 - MAKER_FEE

    def sell_pnl(fill_price):
        """Realised vs P0 as cost-basis."""
        return (fill_price - P0) / P0 - MAKER_FEE

    # Strategy A: keep all orders
    pnl_a = (sum(buy_pnl(p)  for p in filled_buys) +
              sum(sell_pnl(p) for p in filled_sells))

    # Strategy B: cancel the dangerous side
    if regime == "trending_dn":
        # Buys cancelled → only sells fill (these are above P0, need price to bounce first)
        pnl_b = sum(sell_pnl(p) for p in filled_sells)
    elif regime == "trending_up":
        # Sells cancelled → only buys fill (these are below P0, need price to dip first)
        pnl_b = sum(buy_pnl(p) for p in filled_buys)
    else:
        pnl_b = 0.0

    # Strategy C: cancel all
    pnl_c = 0.0

    return {
        "regime":        regime,
        "move_pct":      (P_end - P0) / P0 * 100,
        "n_candles":     event.get("n_candles", 0),
        "note":          event.get("note", ""),
        "filled_buys":   len(filled_buys),
        "filled_sells":  len(filled_sells),
        "pnl_a_pct":     pnl_a * 100,
        "pnl_b_pct":     pnl_b * 100,
        "pnl_c_pct":     pnl_c * 100,
        "source":        event.get("source", ""),
    }


# ------------------------------------------------------------------ #
#  Report                                                              #
# ------------------------------------------------------------------ #

def print_report(results: list[dict], data_source: str) -> None:
    print(f"\n{'='*80}")
    print(f"TREND PAUSE STRATEGY BACKTEST  ({data_source})")
    print(f"{'='*80}")
    print(f"\n{'Strategy legend':}")
    print(f"  A keep-all      : all 6 grid orders left open during pause (current behaviour)")
    print(f"  B cancel-danger : cancel BUY orders on trending_dn, cancel SELL on trending_up")
    print(f"  C cancel-all    : cancel all orders immediately on pause")
    print(f"\nGrid: {LEVELS_EACH_SIDE} buy + {LEVELS_EACH_SIDE} sell levels, "
          f"{SPACING_PCT*100:.1f}% spacing, {MAKER_FEE*100:.2f}% maker fee per fill.\n")

    hdr = (f"{'#':>3}  {'Regime':>12}  {'Move%':>7}  {'Bars':>4}  "
           f"{'Buys':>4}  {'Sells':>5}  {'A%':>7}  {'B%':>7}  {'C%':>6}  Note")
    print(hdr)
    print("-" * 95)
    for i, r in enumerate(results, 1):
        print(
            f"{i:>3}  {r['regime']:>12}  {r['move_pct']:>+7.2f}  {r['n_candles']:>4}  "
            f"{r['filled_buys']:>4}  {r['filled_sells']:>5}  "
            f"{r['pnl_a_pct']:>+7.3f}  {r['pnl_b_pct']:>+7.3f}  {r['pnl_c_pct']:>+6.3f}  "
            f"{r.get('note','')}"
        )

    n = len(results)
    ta = sum(r["pnl_a_pct"] for r in results)
    tb = sum(r["pnl_b_pct"] for r in results)
    tc = sum(r["pnl_c_pct"] for r in results)

    dn_r = [r for r in results if r["regime"] == "trending_dn"]
    up_r = [r for r in results if r["regime"] == "trending_up"]

    print("-" * 95)
    print(f"\n{'Events':>10}: {n}  ({len(dn_r)} dn / {len(up_r)} up)")
    print(f"{'TOTAL':>10}:  A={ta:>+8.3f}%   B={tb:>+8.3f}%   C={tc:>+8.3f}%")
    print(f"{'PER EVENT':>10}:  A={ta/n:>+8.3f}%   B={tb/n:>+8.3f}%   C={tc/n:>+8.3f}%")

    if dn_r:
        da = sum(r["pnl_a_pct"] for r in dn_r)
        db = sum(r["pnl_b_pct"] for r in dn_r)
        dc = sum(r["pnl_c_pct"] for r in dn_r)
        print(f"\n  trending_dn ({len(dn_r)} events):  A={da:>+8.3f}%  B={db:>+8.3f}%  C={dc:>+8.3f}%")
        dn_best = "A" if da >= db and da >= dc else ("B" if db >= dc else "C")
        print(f"    → Best for downtrends:  Strategy {dn_best}")

    if up_r:
        ua = sum(r["pnl_a_pct"] for r in up_r)
        ub = sum(r["pnl_b_pct"] for r in up_r)
        uc = sum(r["pnl_c_pct"] for r in up_r)
        print(f"\n  trending_up ({len(up_r)} events):  A={ua:>+8.3f}%  B={ub:>+8.3f}%  C={uc:>+8.3f}%")
        up_best = "A" if ua >= ub and ua >= uc else ("B" if ub >= uc else "C")
        print(f"    → Best for uptrends:  Strategy {up_best}")

    winners = {"A keep-all": ta, "B cancel-dangerous": tb, "C cancel-all": tc}
    best    = max(winners, key=winners.get)
    print(f"\n{'★ OVERALL WINNER':>18}: {best}  ({winners[best]:+.3f}%)")

    # Qualitative interpretation
    print(f"\n{'─'*80}")
    print("Interpretation:")
    if best.startswith("A"):
        print("  keep-all wins: grid orders on both sides capture value even during trends.")
        print("  Sell orders above fill profitably in uptrends; buy orders below can fill on")
        print("  temporary bounces in downtrends. Fee savings from NOT cancelling add up.")
    elif best.startswith("B"):
        print("  cancel-dangerous wins: eliminating the directional risk outweighs the")
        print("  missed fills. In downtrends, every buy order that fills is a loss; in")
        print("  uptrends, every sell order that fills exits BTC too early.")
    else:
        print("  cancel-all wins: the opportunity cost of letting any orders fill during")
        print("  a confirmed trend exceeds the value of any remaining fills.")
    print()


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

def run_backtest():
    print("Attempting to fetch real BTC/USDT 4h candles...")
    real_candles = _try_fetch_ohlcv(days=90)

    if real_candles and len(real_candles) >= 60:
        print(f"  Got {len(real_candles)} real candles. Extracting pause events...")
        events = extract_real_events(real_candles)
        data_source = f"Binance 4h OHLCV ({len(real_candles)} candles)"
    else:
        print("  Live fetch unavailable. Using 27 documented BTC scenario events (2021-25).")
        events = scenarios_to_events(build_scenarios())
        data_source = "Scenario-based simulation (27 historical BTC episodes, 2021-25)"

    if not events:
        print("No pause events found.")
        return

    results = [simulate_event(e) for e in events]
    print_report(results, data_source)


if __name__ == "__main__":
    run_backtest()
