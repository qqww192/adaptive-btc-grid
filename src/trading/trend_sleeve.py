"""
Trend sleeve — a small, complementary spot trend-follower (DISABLED by default).

Why
---
The grid earns in ranging markets. Even after the trend-pause fix lets the grid
keep trading a wider grid during trends, a *strong, clean* up-trend is still a
regime where a long trend-follow position out-earns a mean-reverting grid. This
module runs a single, bounded, **spot long-only, maker-only** position that:

  • enters once on a CONFIRMED up-trend (regime_classifier: confirmed_trend),
  • rides it, tracking the peak,
  • exits on a volatility-scaled trailing stop OR when the trend ends.

It is deliberately conservative and OFF by default. Enable only after paper
testing by setting  "trend_sleeve_enabled": true  in config/grid_params.json.

Safety / constraints (same as the grid)
---------------------------------------
  • Spot only, no leverage, long only (never shorts).
  • POST_ONLY / maker orders only.
  • Bounded notional = total_capital * sleeve_pct (default 10%), one position max.
  • Realised P&L is reported to risk_manager.record_trade so the weekly kill
    switch governs the sleeve too.
  • Own state file (data/trend_sleeve_state.json) — never touches weekly_state
    or the trades ledger format.

The planner (`plan_action`) is a PURE function (no network) and is unit-tested in
the __main__ block; the executor (`run_sleeve`) is a thin, strictly-gated wrapper.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

STATE_FILE   = ROOT / "data" / "trend_sleeve_state.json"
MIN_QTY_BTC  = 0.0001

SLEEVE_PCT_DEFAULT       = 0.10   # fraction of total_capital allocated to the sleeve
TRAIL_MIN_PCT            = 5.0    # trailing-stop floor (same philosophy as the grid)
TRAIL_ATR_MULT           = 1.5    # trail = max(floor, mult × daily ATR%)


# ------------------------------------------------------------------ #
#  State helpers                                                       #
# ------------------------------------------------------------------ #

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"position": "FLAT", "qty": 0.0, "entry_price": 0.0, "peak_price": 0.0}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state, indent=2))
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _trail_pct(regime_data: dict, price: float) -> float:
    atr_usdt = float(regime_data.get("atr", 0.0) or 0.0)
    atr_pct  = (atr_usdt / price * 100) if price else 0.0
    return max(TRAIL_MIN_PCT, TRAIL_ATR_MULT * atr_pct)


# ------------------------------------------------------------------ #
#  Pure planner (no network — unit tested)                            #
# ------------------------------------------------------------------ #

def plan_action(state: dict, regime_data: dict, price: float,
                config: dict) -> dict | None:
    """
    Decide the sleeve's next action. Pure: no I/O, no side effects.

    Returns one of:
      {"action": "enter", "qty": q, "price": p}
      {"action": "exit",  "qty": q, "price": p, "reason": str}
      {"action": "hold",  "peak_price": p}        # update trailing peak, no order
      None                                          # nothing to do
    """
    regime          = regime_data.get("regime", "ranging")
    confirmed_trend = bool(regime_data.get("confirmed_trend", False))
    position        = state.get("position", "FLAT")

    if position == "FLAT":
        # Enter once on a confirmed up-trend.
        if confirmed_trend and regime == "trending_up" and price > 0:
            sleeve_pct    = float(config.get("trend_sleeve_pct", SLEEVE_PCT_DEFAULT))
            gbp_usd       = float(config.get("gbp_usd_rate", 1.27)) or 1.27
            notional_usdt = float(config.get("total_capital", 0)) * sleeve_pct * gbp_usd
            qty           = round(notional_usdt / price, 6) if price else 0.0
            if qty >= MIN_QTY_BTC:
                return {"action": "enter", "qty": qty, "price": price}
        return None

    # IN POSITION
    peak      = max(float(state.get("peak_price", 0.0)), price)
    trail_pct = _trail_pct(regime_data, price)
    stop      = peak * (1 - trail_pct / 100)

    # Exit when the trend ends OR the trailing stop is hit.
    if regime != "trending_up":
        return {"action": "exit", "qty": float(state.get("qty", 0.0)),
                "price": price, "reason": f"trend ended (regime={regime})"}
    if price <= stop:
        return {"action": "exit", "qty": float(state.get("qty", 0.0)),
                "price": price, "reason": f"trailing stop {trail_pct:.1f}% below peak ${peak:,.0f}"}

    return {"action": "hold", "peak_price": peak}


# ------------------------------------------------------------------ #
#  Thin executor (strictly gated; maker-only)                         #
# ------------------------------------------------------------------ #

def run_sleeve(cdx, regime_data: dict, price: float, best_bid: float,
               best_ask: float, config: dict, instrument: str = "BTC_USDT") -> None:
    """
    Execute the planned sleeve action with POST_ONLY maker orders. Strict no-op
    unless config["trend_sleeve_enabled"] is true. Mutates/saves its own state.
    Realised P&L on exit is reported to risk_manager.record_trade.
    """
    if not config.get("trend_sleeve_enabled", False):
        return

    from trading.risk_manager import record_trade

    state  = _load_state()
    action = plan_action(state, regime_data, price, config)
    if not action:
        return

    if action["action"] == "hold":
        state["peak_price"] = action["peak_price"]
        _save_state(state)
        return

    if action["action"] == "enter":
        # Maker buy just below best bid so it rests in the book (POST_ONLY).
        limit = round((best_bid or price) * 0.9999, 2)
        try:
            cdx.place_limit_order(instrument, "BUY", limit, action["qty"])
        except Exception as e:
            print(f"[sleeve] enter failed: {e}")
            return
        state.update({"position": "LONG", "qty": action["qty"], "entry_price": limit,
                      "peak_price": price,
                      "opened_at": datetime.now(timezone.utc).isoformat()})
        _save_state(state)
        print(f"[sleeve] ENTER {action['qty']} BTC @ {limit:,.2f} (confirmed up-trend)")
        return

    if action["action"] == "exit":
        # Maker sell just above best ask so it rests in the book (POST_ONLY).
        limit = round((best_ask or price) * 1.0001, 2)
        qty   = action["qty"]
        try:
            cdx.place_limit_order(instrument, "SELL", limit, qty)
        except Exception as e:
            print(f"[sleeve] exit failed: {e}")
            return
        entry   = float(state.get("entry_price", limit))
        gbp_usd = float(config.get("gbp_usd_rate", 1.27)) or 1.27
        net_gbp = (limit - entry) * qty / gbp_usd
        try:
            record_trade(net_gbp)        # kill switch governs the sleeve too
        except Exception as e:
            print(f"[sleeve] record_trade failed: {e}")
        _save_state({"position": "FLAT", "qty": 0.0, "entry_price": 0.0, "peak_price": 0.0})
        print(f"[sleeve] EXIT {qty} BTC @ {limit:,.2f} | net £{net_gbp:+.2f} "
              f"| {action['reason']}")


# ------------------------------------------------------------------ #
#  Self-test (pure planner)                                           #
# ------------------------------------------------------------------ #

def _selftest() -> bool:
    cfg = {"total_capital": 170.0, "gbp_usd_rate": 1.27, "trend_sleeve_pct": 0.10}
    ok = True

    # 1. FLAT + confirmed up-trend → enter
    a = plan_action({"position": "FLAT"},
                    {"regime": "trending_up", "confirmed_trend": True, "atr": 1500},
                    60000.0, cfg)
    ok &= a and a["action"] == "enter" and a["qty"] >= MIN_QTY_BTC

    # 2. FLAT + unconfirmed trend → no entry
    b = plan_action({"position": "FLAT"},
                    {"regime": "trending_up", "confirmed_trend": False}, 60000.0, cfg)
    ok &= b is None

    # 3. LONG + trend continues above stop → hold (peak updates)
    st = {"position": "LONG", "qty": 0.0004, "entry_price": 60000, "peak_price": 60000}
    c = plan_action(st, {"regime": "trending_up", "confirmed_trend": True, "atr": 1200},
                    63000.0, cfg)
    ok &= c and c["action"] == "hold" and c["peak_price"] == 63000.0

    # 4. LONG + deep pullback below trailing stop → exit
    st2 = {"position": "LONG", "qty": 0.0004, "entry_price": 60000, "peak_price": 70000}
    d = plan_action(st2, {"regime": "trending_up", "confirmed_trend": True, "atr": 1000},
                    63000.0, cfg)   # >5% below 70k peak
    ok &= d and d["action"] == "exit"

    # 5. LONG + trend ends → exit
    e = plan_action(st2, {"regime": "ranging", "confirmed_trend": False}, 69000.0, cfg)
    ok &= e and e["action"] == "exit"

    print(f"trend_sleeve planner self-test: {'PASS' if ok else 'FAIL'}")
    return bool(ok)


if __name__ == "__main__":
    sys.exit(0 if _selftest() else 1)
