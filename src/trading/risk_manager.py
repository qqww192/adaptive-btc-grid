"""
Risk manager — enforces the weekly kill switch, tracks P&L, and
provides Skill 4: CDaR-based dynamic capital sizing.

Kill switch logic
-----------------
  - Weekly P&L is computed as the sum of all net gains/losses since
    Monday 00:00 UTC.
  - If weekly_pnl <= -(total_capital * kill_pct), trading halts.
  - The state resets automatically on Monday 00:00 UTC.
  - A Telegram alert is sent when the switch triggers.

Skill 4: Dynamic capital sizing (Riskfolio-Lib / CDaR)
--------------------------------------------------------
  get_dynamic_capital_pct() reads the last 30 SELLs, computes
  Conditional Drawdown at Risk (CDaR), and returns a stepped-down
  capital_pct when drawdown is deepening — eliminating the cliff-edge
  binary kill switch.

  Steps:
    CDaR < 30% of kill threshold  → full deployment (base_capital_pct)
    CDaR 30–50%                   → base − 0.10
    CDaR 50–80%                   → base − 0.15
    CDaR > 80%                    → base − 0.30  (minimum 0.40)
"""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT        = Path(__file__).resolve().parents[2]
STATE_FILE  = ROOT / "data"   / "weekly_state.json"
CONFIG_FILE = ROOT / "config" / "grid_params.json"

_TOTAL_CAPITAL_GBP_DEFAULT = float(os.environ.get("TOTAL_CAPITAL_GBP", "150"))


def _get_total_capital() -> float:
    """Read total_capital from config first, fall back to env var.
    Keeps kill threshold in sync when capital is topped up in config."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        return float(cfg.get("total_capital", _TOTAL_CAPITAL_GBP_DEFAULT))
    except Exception:
        return _TOTAL_CAPITAL_GBP_DEFAULT


# Module-level alias kept for backward compat with any direct references.
TOTAL_CAPITAL_GBP = _TOTAL_CAPITAL_GBP_DEFAULT


def _get_kill_pct() -> float:
    """Read kill_pct from config file first, fall back to env var.
    This ensures the Sunday AI optimizer's regime-aware kill_pct changes take effect."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        return float(cfg.get("kill_pct", os.environ.get("KILL_SWITCH_PCT", "0.10")))
    except Exception:
        return float(os.environ.get("KILL_SWITCH_PCT", "0.10"))


# ------------------------------------------------------------------ #
#  Weekly state helpers                                                #
# ------------------------------------------------------------------ #

def _monday_utc() -> datetime:
    """Return the most recent Monday 00:00:00 UTC."""
    now   = datetime.now(timezone.utc)
    delta = timedelta(days=now.weekday())
    return (now - delta).replace(hour=0, minute=0, second=0, microsecond=0)


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically: temp file in same dir then os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            # Corrupted file — fail safe: force kill switch ON so the bot does
            # not trade through a week where the threshold may already be breached.
            _send_telegram(
                "🚨 *CRITICAL: weekly_state.json is corrupted*\n"
                "Kill switch forced ACTIVE as a safety measure.\n"
                "Delete data/weekly_state.json to reset (will lose weekly P&L counter)."
            )
            print("[risk] CRITICAL: weekly_state.json corrupted — kill switch forced ON")
            return {"kill_switch_on": True, "corrupted": True}
    return {}


def _save(state: dict) -> None:
    _atomic_write(STATE_FILE, json.dumps(state, indent=2))


def _reset_if_new_week(state: dict) -> dict:
    monday = _monday_utc().isoformat()
    if state.get("week_start") != monday:
        state = {
            "week_start":       monday,
            "weekly_pnl_gbp":   0.0,
            "kill_switch_on":   False,
            "kill_trigger_at":  None,
            "trades_this_week": 0,
            "warning_sent":     False,
            "ai_reduce_cap":    False,
        }
        _save(state)
    return state


def get_state() -> dict:
    """Load, reset-if-needed, and return the current weekly state."""
    return _reset_if_new_week(_load())


def is_kill_switch_active() -> bool:
    """Return True if trading should be halted this week."""
    return get_state().get("kill_switch_on", False)


def record_trade(net_pnl_gbp: float) -> dict:
    """
    Add a completed trade's net P&L to the weekly total and check
    whether the kill switch should fire. Returns the updated state.
    """
    state                     = get_state()
    state["weekly_pnl_gbp"]   += net_pnl_gbp
    state["trades_this_week"] += 1

    kill_pct       = _get_kill_pct()
    kill_threshold = -(_get_total_capital() * kill_pct)

    if state["weekly_pnl_gbp"] <= kill_threshold and not state["kill_switch_on"]:
        state["kill_switch_on"]  = True
        state["kill_trigger_at"] = datetime.now(timezone.utc).isoformat()
        _save(state)
        _send_kill_alert(state)
    else:
        _save(state)

    return state


def record_warning(state: dict) -> None:
    """Send a Telegram warning when P&L hits the 50%-of-kill threshold.
    Also asks AI whether to reduce capital deployment."""
    # Always read fresh state — the passed-in state may be stale if multiple
    # fills were processed in the same run before this is called.
    state = get_state()
    if state.get("warning_sent"):
        return
    kill_pct       = _get_kill_pct()
    kill_abs       = _get_total_capital() * kill_pct
    warning_thresh = -(kill_abs * 0.5)
    if state["weekly_pnl_gbp"] <= warning_thresh:
        # Priority 4: ask AI whether to reduce capital
        ai_decision = "hold"
        try:
            from trading.trade_logger import read_since
            from trading.ai_advisor   import ask_kill_switch_guardian
            from datetime import timedelta
            since_7d    = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            sells       = [t for t in read_since(since_7d) if t.get("side") == "SELL"]
            net_returns = [t["net_gbp"] for t in sells[-20:]]
            ai_decision = ask_kill_switch_guardian(
                weekly_pnl         = state["weekly_pnl_gbp"],
                kill_threshold     = -kill_abs,
                recent_net_returns = net_returns,
            )
        except Exception as e:
            print(f"[risk] AI guardian failed: {e}")

        action_note = (
            "🤖 AI recommends: *reduce capital deployment*"
            if ai_decision == "reduce"
            else "🤖 AI recommends: *hold deployment* (likely temporary)"
        )
        _send_telegram(
            f"⚠️ *Grid bot warning*\n"
            f"Weekly P&L: £{state['weekly_pnl_gbp']:.2f} "
            f"(50% of kill switch threshold)\n"
            f"Kill switch triggers at -£{kill_abs:.2f}\n"
            f"{action_note}"
        )
        state["warning_sent"]  = True
        state["ai_reduce_cap"] = (ai_decision == "reduce")
        _save(state)


# ------------------------------------------------------------------ #
#  Skill 4: CDaR-based dynamic capital sizing                          #
# ------------------------------------------------------------------ #

def _compute_cdar(net_returns: list[float], alpha: float = 0.95) -> float:
    """
    Conditional Drawdown at Risk at confidence level alpha (numpy fallback).

    CDaR = average drawdown in the worst (1-alpha)% of observations.
    """
    if len(net_returns) < 5:
        return 0.0
    try:
        import numpy as np
        arr         = np.array(net_returns, dtype=float)
        cumulative  = np.cumsum(arr)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns   = cumulative - running_max
        threshold   = float(np.percentile(drawdowns, (1.0 - alpha) * 100))
        tail        = drawdowns[drawdowns <= threshold]
        return float(abs(tail.mean())) if len(tail) > 0 else 0.0
    except Exception:
        return 0.0


def _try_riskfolio_cdar(net_returns: list[float]) -> Optional[float]:
    """
    Use Riskfolio-Lib's CDaR if installed — provides CVXPY-optimised
    risk measures.  Returns None if riskfolio-lib is not available.
    """
    try:
        import riskfolio as rp
        import pandas as pd
        import numpy as np
        series = pd.Series(net_returns, name="asset")
        w      = pd.Series([1.0], index=["asset"])
        cdar   = rp.RiskFunctions.CDaR_Abs(
            series.values.reshape(-1, 1), w.values, alpha=0.95
        )
        return float(cdar)
    except Exception:
        return None


def get_dynamic_capital_pct(base_capital_pct: float = 0.70) -> float:
    """
    Return a CDaR-adjusted capital_pct.

    Reads the last 30 SELL trades, computes Conditional Drawdown at Risk,
    and scales down deployment before the binary kill switch fires —
    giving the bot a softer landing.

    Guaranteed to stay within [0.40, base_capital_pct].
    """
    try:
        from trading.trade_logger import read_since
        from datetime import timedelta
        since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        sells     = [t for t in read_since(since_30d) if t.get("side") == "SELL"]
        net_rets  = [t["net_gbp"] for t in sells[-30:]]
    except Exception:
        return base_capital_pct

    if len(net_rets) < 5:
        return base_capital_pct

    cdar = _try_riskfolio_cdar(net_rets)
    if cdar is None:
        cdar = _compute_cdar(net_rets)

    kill_pct = _get_kill_pct()
    kill_abs = _get_total_capital() * kill_pct
    ratio    = cdar / kill_abs if kill_abs else 0.0

    if ratio > 0.80:
        adjusted = max(0.40, base_capital_pct - 0.30)
    elif ratio > 0.50:
        adjusted = max(0.55, base_capital_pct - 0.15)
    elif ratio > 0.30:
        adjusted = max(0.60, base_capital_pct - 0.10)
    else:
        adjusted = base_capital_pct

    # Priority 4: apply extra step-down if AI guardian recommended reducing capital
    state = get_state()
    if state.get("ai_reduce_cap") and adjusted > 0.50:
        adjusted = max(0.50, adjusted - 0.10)
        print(f"[risk] AI guardian step-down applied → capital_pct={adjusted:.2f}")

    if adjusted < base_capital_pct:
        print(
            f"[risk] CDaR={cdar:.3f} GBP ({ratio*100:.0f}% of kill threshold) "
            f"→ capital_pct stepped down {base_capital_pct:.2f} → {adjusted:.2f}"
        )
    return adjusted


# ------------------------------------------------------------------ #
#  Telegram helpers                                                    #
# ------------------------------------------------------------------ #

def _send_kill_alert(state: dict) -> None:
    kill_pct = _get_kill_pct()
    _send_telegram(
        f"🛑 *Kill switch triggered*\n"
        f"Weekly P&L reached £{state['weekly_pnl_gbp']:.2f} "
        f"(limit: -£{_get_total_capital() * kill_pct:.2f})\n"
        f"Trading paused until Monday 00:00 UTC.\n"
        f"Trades this week: {state['trades_this_week']}"
    )


def _send_telegram(message: str) -> None:
    """Fire-and-forget Telegram message. Errors are logged, not raised."""
    import httpx
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"[risk] Telegram not configured — message: {message}")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as exc:
        print(f"[risk] Telegram send failed: {exc}")
