"""
Grid trader — the core bot, run every 5 minutes by crontab.

What it does each run
---------------------
1. Exit immediately if kill switch is active.
2. Exit immediately if data/paused.flag exists (Telegram controller).
3. Load current grid params from config/grid_params.json.
4. Apply regime-recommended params if HMM confidence ≥ 0.7 (Skill 2 fix).
5. Apply CDaR-adjusted capital_pct (Skill 4).
6. Fetch current BTC price.
7. Detect any orders that filled since the last run.
8. Log fills, update risk manager, place replacement orders.
9. Check if the grid band needs recentering (price moved > 3%).
10. Emit Prometheus metrics to data/prometheus/grid_trader.prom (Skill 6).

State between runs
------------------
data/grid_state.json tracks:
  - Current grid levels and their order IDs
  - The buy price at each level (needed to compute SELL profit)
  - Last run timestamp
  - Last calibration price (for range-break detection)

Locking
-------
A PID lock file (data/grid_trader.lock) prevents concurrent runs
in case a slow API call causes two cron instances to overlap.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trading.cdx_client  import CDXClient, CDXError
from trading.risk_manager import (
    get_state           as get_risk_state,
    get_dynamic_capital_pct,
    is_kill_switch_active,
    record_trade,
    record_warning,
)
from trading.trade_logger import append as log_trade
from trading.ai_advisor import (
    ask_recenter,
    ask_regime,
    send_monday_briefing,
)

# ------------------------------------------------------------------ #
#  Paths                                                               #
# ------------------------------------------------------------------ #
CONFIG_FILE     = ROOT / "config" / "grid_params.json"
REGIME_FILE     = ROOT / "data"   / "regime.json"
GRID_STATE_FILE = ROOT / "data"   / "grid_state.json"
LOCK_FILE       = ROOT / "data"   / "grid_trader.lock"
HEARTBEAT_FILE  = ROOT / "data"   / "last_heartbeat.json"
PAUSE_FLAG      = ROOT / "data"   / "paused.flag"        # Telegram controller
RECENTER_FLAG   = ROOT / "data"   / "force_recenter.flag" # Telegram controller
PROM_DIR        = ROOT / "data"   / "prometheus"

RECENTER_CONFIRM_MINUTES = 20  # force recenter if AI keeps saying HOLD past this

SAFE_BOUNDS = {
    "spacing_pct": (0.55, 3.0),
    "range_pct":   (2.0,  15.0),
    "levels":      (4,    20),
    "capital_pct": (0.40, 0.80),
    "kill_pct":    (0.05, 0.15),
}

INSTRUMENT = "BTC_USDT"


# ------------------------------------------------------------------ #
#  Skill 6: Prometheus metrics (textfile for cron-based processes)     #
# ------------------------------------------------------------------ #

def _emit_prometheus(
    price:        float,
    risk_state:   dict,
    config:       dict,
    regime:       str,
    active_orders: int,
    hmm_confidence: float,
) -> None:
    """
    Write Prometheus metrics to data/prometheus/grid_trader.prom.
    node_exporter reads this directory with --collector.textfile.directory.
    Silently skipped if prometheus_client is not installed.
    """
    try:
        from prometheus_client import CollectorRegistry, Gauge, write_to_textfile
    except ImportError:
        return

    reg = CollectorRegistry()

    def g(name, desc):
        return Gauge(name, desc, registry=reg)

    g("btc_grid_btc_price_usdt",   "Current BTC/USDT price").set(price)
    g("btc_grid_pnl_weekly_gbp",   "Weekly net P&L in GBP").set(
        risk_state.get("weekly_pnl_gbp", 0)
    )
    g("btc_grid_kill_switch_active", "1 if kill switch is active").set(
        1 if risk_state.get("kill_switch_on") else 0
    )
    g("btc_grid_active_orders",    "Number of live grid orders").set(active_orders)
    g("btc_grid_spacing_pct",      "Current grid spacing %").set(
        config.get("spacing_pct", 0)
    )
    g("btc_grid_capital_pct",      "Capital deployment fraction").set(
        config.get("capital_pct", 0)
    )
    g("btc_grid_levels",           "Number of grid levels").set(
        config.get("levels", 0)
    )
    g("btc_grid_hmm_confidence",   "HMM regime confidence 0-1").set(hmm_confidence)
    g("btc_grid_trades_this_week", "Trade count this week").set(
        risk_state.get("trades_this_week", 0)
    )

    try:
        PROM_DIR.mkdir(parents=True, exist_ok=True)
        write_to_textfile(str(PROM_DIR / "grid_trader.prom"), reg)
    except Exception as e:
        print(f"[grid] Prometheus write failed: {e}")


# ------------------------------------------------------------------ #
#  Lock / unlock                                                       #
# ------------------------------------------------------------------ #

def acquire_lock() -> bool:
    """Return False if another run is already in progress."""
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 90:
            print("[grid] Lock file exists — previous run still active. Exiting.")
            return False
        print("[grid] Stale lock file removed.")
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock() -> None:
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


# ------------------------------------------------------------------ #
#  Config helpers                                                      #
# ------------------------------------------------------------------ #

def load_config() -> dict:
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text())
    else:
        config = {
            "instrument":    "BTC_USDT",
            "spacing_pct":   0.8,
            "range_pct":     5.0,
            "levels":        10,
            "capital_pct":   0.70,
            "total_capital": float(os.environ.get("TOTAL_CAPITAL_GBP", "150")),
            "gbp_usd_rate":  float(os.environ.get("GBP_USD_RATE", "1.27")),
        }
    violations = []
    for key, (lo, hi) in SAFE_BOUNDS.items():
        val = config.get(key)
        if val is None:
            violations.append(f"{key} missing")
        elif not (lo <= val <= hi):
            violations.append(f"{key}={val} outside [{lo}, {hi}]")
    if violations:
        msg = "[grid] UNSAFE CONFIG — refusing to trade: " + "; ".join(violations)
        print(msg)
        _send_telegram_alert(msg)
        sys.exit(1)
    return config


def load_grid_state() -> dict:
    if GRID_STATE_FILE.exists():
        try:
            return json.loads(GRID_STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"levels": {}, "calibration_price": None, "last_run": None}


def save_grid_state(state: dict) -> None:
    GRID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GRID_STATE_FILE.write_text(json.dumps(state, indent=2))


def load_regime() -> dict:
    if REGIME_FILE.exists():
        try:
            return json.loads(REGIME_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"regime": "ranging", "hmm_confidence": 0.0, "recommended": {}}


# ------------------------------------------------------------------ #
#  Skill 2 fix: apply regime-recommended params when HMM is confident  #
# ------------------------------------------------------------------ #

HMM_CONFIDENCE_THRESHOLD = 0.70  # minimum confidence to trust HMM regime params

def apply_regime_params(config: dict, regime_data: dict) -> dict:
    """
    Blend regime-recommended params into the base config when the HMM
    confidence is high enough. This activates the previously dead-code
    regime.json recommended params.

    Only spacing_pct, levels, and capital_pct are blended — kill_pct and
    range_pct stay under Gemini/Optuna control to avoid oscillation.
    """
    confidence  = float(regime_data.get("hmm_confidence", 0.0))
    recommended = regime_data.get("recommended", {})
    regime      = regime_data.get("regime", "ranging")

    if not recommended or confidence < HMM_CONFIDENCE_THRESHOLD:
        return config

    blended = dict(config)
    for key in ("spacing_pct", "levels", "capital_pct"):
        if key in recommended:
            blended[key] = recommended[key]

    print(
        f"[grid] Regime override ({regime}, conf={confidence:.2f}): "
        f"spacing={blended['spacing_pct']}% levels={blended['levels']} "
        f"capital_pct={blended['capital_pct']}"
    )
    return blended


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

MIN_QTY_BTC = 0.0001  # crypto.com minimum order size


def _send_telegram_alert(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  Grid maths                                                          #
# ------------------------------------------------------------------ #

def build_grid(center_price: float, config: dict) -> list[dict]:
    """
    Generate grid levels around center_price.

    Returns a list of dicts, each with:
      level     — index (0 = bottom buy, levels-1 = top sell)
      price     — limit price for this order
      side      — BUY (below center) or SELL (above center)
      qty_btc   — BTC quantity per level
    """
    spacing_pct    = config["spacing_pct"] / 100
    levels         = config["levels"]
    capital_gbp    = config["total_capital"] * config["capital_pct"]
    gbp_usd        = config["gbp_usd_rate"]
    capital_usdt   = capital_gbp * gbp_usd
    per_level_usdt = capital_usdt / levels

    result = []
    half   = levels // 2
    for i in range(levels):
        offset = (i - half) * spacing_pct
        price  = center_price * (1 + offset)
        side   = "SELL" if i >= half else "BUY"
        qty    = per_level_usdt / price
        if qty < MIN_QTY_BTC:
            print(f"[grid] Level {i}: qty {qty:.8f} BTC below minimum {MIN_QTY_BTC} — flooring")
            qty = MIN_QTY_BTC
        result.append({
            "level": i,
            "price": round(price, 2),
            "side":  side,
            "qty":   round(qty, 6),
        })
    return result


# ------------------------------------------------------------------ #
#  Order placement                                                     #
# ------------------------------------------------------------------ #

def place_grid(cdx: CDXClient, levels: list[dict], grid_state: dict) -> None:
    """Place limit orders for each grid level. Skips already-live levels."""
    for lvl in levels:
        idx = str(lvl["level"])
        if idx in grid_state["levels"] and grid_state["levels"][idx].get("order_id"):
            continue
        try:
            order_id = cdx.place_limit_order(
                INSTRUMENT, lvl["side"], lvl["price"], lvl["qty"]
            )
            grid_state["levels"][idx] = {
                "order_id":  order_id,
                "side":      lvl["side"],
                "price":     lvl["price"],
                "qty":       lvl["qty"],
                "placed_at": datetime.now(timezone.utc).isoformat(),
                "buy_price": lvl["price"] if lvl["side"] == "BUY" else None,
            }
            print(f"[grid] Placed {lvl['side']} {lvl['qty']:.6f} BTC @ {lvl['price']:.2f} "
                  f"(level {idx})")
        except (CDXError, Exception) as e:
            print(f"[grid] Failed to place level {idx}: {e}")


# ------------------------------------------------------------------ #
#  Fill detection                                                      #
# ------------------------------------------------------------------ #

def detect_fills(
    cdx:        CDXClient,
    grid_state: dict,
    config:     dict,
    regime:     str,
    risk_state: dict,
) -> None:
    """
    Cross-reference open orders with order history.
    For each filled order: log it, update risk, schedule replacement.
    """
    try:
        history = cdx.get_order_history(INSTRUMENT, limit=50)
    except Exception as e:
        print(f"[grid] Failed to fetch order history: {e} — skipping fill detection")
        return
    filled     = {o["order_id"]: o for o in history if o.get("status") == "FILLED"}
    week_start = risk_state.get("week_start", "")

    for idx, info in list(grid_state["levels"].items()):
        oid = info.get("order_id")
        if not oid or oid not in filled:
            continue

        order  = filled[oid]
        side   = info["side"]
        price  = float(order.get("avg_price", info["price"]))
        qty    = float(order.get("cumulative_quantity", info["qty"]))
        fee    = float(order.get("cumulative_fee", 0))
        buy_px = info.get("buy_price") if side == "SELL" else None

        entry = log_trade(
            order_id       = oid,
            side           = side,
            price_usdt     = price,
            qty_btc        = qty,
            fee_usdt       = fee,
            regime         = regime,
            grid_level     = int(idx),
            week_start     = week_start,
            buy_price_usdt = buy_px,
        )

        if side == "SELL":
            risk_state = record_trade(entry["net_gbp"])
            print(f"[grid] SELL filled @ {price:.2f} | net P&L: £{entry['net_gbp']:.4f}")
        else:
            print(f"[grid] BUY filled @ {price:.2f}")

        grid_state["levels"][idx] = {}

    record_warning(risk_state)


# ------------------------------------------------------------------ #
#  Range-break check                                                   #
# ------------------------------------------------------------------ #

def needs_recalibration(current_price: float, grid_state: dict, config: dict) -> bool:
    cal_price = grid_state.get("calibration_price")
    if cal_price is None or cal_price <= 0:
        return True
    move_pct = abs(current_price - cal_price) / cal_price * 100
    return move_pct > 3.0


def cancel_all_and_clear(cdx: CDXClient, grid_state: dict) -> None:
    try:
        cdx.cancel_all_orders(INSTRUMENT)
    except CDXError as e:
        print(f"[grid] cancel_all_orders failed: {e}")
    grid_state["levels"] = {}


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def run() -> None:
    if not acquire_lock():
        return
    try:
        _run()
    finally:
        release_lock()


def _run() -> None:
    # 1. Kill switch check
    if is_kill_switch_active():
        print("[grid] Kill switch is active — bot paused until Monday. Exiting.")
        return

    # 2. Pause flag check (set by Telegram controller — Skill 3)
    if PAUSE_FLAG.exists():
        print("[grid] Pause flag active — bot paused by Telegram command. Exiting.")
        return

    risk_state  = get_risk_state()
    config      = load_config()
    regime_data = load_regime()
    regime      = regime_data.get("regime", "ranging")
    hmm_conf    = float(regime_data.get("hmm_confidence", 0.0))

    # 3. Apply regime-recommended params if HMM is confident (Skill 2 fix)
    config = apply_regime_params(config, regime_data)

    # 4. CDaR-adjusted capital_pct (Skill 4)
    config["capital_pct"] = get_dynamic_capital_pct(config["capital_pct"])

    grid_state = load_grid_state()
    cdx        = CDXClient()

    # 5. Current price
    try:
        ticker = cdx.get_ticker(INSTRUMENT)
    except (CDXError, httpx.TimeoutException, Exception) as e:
        print(f"[grid] get_ticker failed: {e} — aborting run.")
        return

    price = ticker["price"]

    # Priority 2: AI regime override when HMM confidence is low
    recent_candles: list[dict] = []
    if hmm_conf < HMM_CONFIDENCE_THRESHOLD:
        try:
            recent_candles = cdx.get_candlesticks(INSTRUMENT, "4h", count=8)
            ai_regime      = ask_regime(recent_candles, regime, hmm_conf)
            if ai_regime != regime:
                regime              = ai_regime
                regime_data["regime"] = regime
                config              = apply_regime_params(config, regime_data)
        except Exception as e:
            print(f"[grid] AI regime fetch failed: {e} — using HMM regime")

    print(f"[grid] BTC/USDT: {price:,.2f} | Regime: {regime} (HMM conf={hmm_conf:.2f})")

    # 6. Detect fills from previous orders
    detect_fills(cdx, grid_state, config, regime, risk_state)

    # Re-check kill switch — a fill may have triggered it
    if is_kill_switch_active():
        print("[grid] Kill switch triggered by fill — cancelling all orders.")
        cancel_all_and_clear(cdx, grid_state)
        save_grid_state(grid_state)
        return

    # Priority 3: Monday morning AI briefing (fires once on weekly reset)
    last_seen_week = grid_state.get("last_seen_week_start")
    current_week   = risk_state.get("week_start", "")
    if current_week and last_seen_week != current_week:
        grid_state["last_seen_week_start"] = current_week
        try:
            send_monday_briefing(
                last_week_pnl    = float(risk_state.get("weekly_pnl_gbp", 0)),
                last_week_trades = int(risk_state.get("trades_this_week", 0)),
                current_regime   = regime,
                config           = config,
            )
        except Exception as e:
            print(f"[grid] Monday briefing failed: {e}")

    # 7. Priority 1: Smart recenter — AI confirms before acting
    force = RECENTER_FLAG.exists()
    if force:
        RECENTER_FLAG.unlink()
        print("[grid] Force-recentre flag set by Telegram — recentering now.")

    do_recenter = False
    if force:
        do_recenter = True
    elif needs_recalibration(price, grid_state, config):
        # Lazy-fetch candles if not already loaded for regime override
        if not recent_candles:
            try:
                recent_candles = cdx.get_candlesticks(INSTRUMENT, "4h", count=6)
            except Exception:
                recent_candles = []

        if ask_recenter(price, grid_state.get("calibration_price", price), recent_candles, regime):
            do_recenter = True
            grid_state.pop("outside_since", None)
        else:
            # AI says hold — track how long we've been outside threshold
            if "outside_since" not in grid_state:
                grid_state["outside_since"] = datetime.now(timezone.utc).isoformat()
                print(f"[grid] Smart recenter: AI says HOLD — watching for reversion")
            else:
                try:
                    outside_dt  = datetime.fromisoformat(grid_state["outside_since"])
                    elapsed_min = (datetime.now(timezone.utc) - outside_dt).total_seconds() / 60
                    print(f"[grid] Smart recenter: AI holding ({elapsed_min:.0f}/{RECENTER_CONFIRM_MINUTES}min)")
                    if elapsed_min >= RECENTER_CONFIRM_MINUTES:
                        print(f"[grid] Smart recenter: timeout reached — forcing recenter")
                        do_recenter = True
                        grid_state.pop("outside_since", None)
                except Exception:
                    do_recenter = True
    else:
        grid_state.pop("outside_since", None)

    if do_recenter:
        print(f"[grid] Recentering grid around {price:,.2f}")
        cancel_all_and_clear(cdx, grid_state)
        grid_state["calibration_price"] = price

    # Capital sufficiency check
    min_cap_usdt = MIN_QTY_BTC * price * config["levels"]
    min_cap_gbp  = min_cap_usdt / config["capital_pct"] / config.get("gbp_usd_rate", 1.27)
    if config["total_capital"] < min_cap_gbp:
        msg = (
            f"[grid] INSUFFICIENT CAPITAL — need ≥£{min_cap_gbp:.0f} for "
            f"{config['levels']} levels at BTC ${price:,.0f}. "
            f"Have £{config['total_capital']}. "
            f"Reduce 'levels' in config or deposit more capital."
        )
        print(msg)
        _send_telegram_alert(msg)
        sys.exit(1)

    # 8. Build and place grid
    levels = build_grid(price, config)
    place_grid(cdx, levels, grid_state)

    # Count active orders for Prometheus
    active_orders = sum(
        1 for info in grid_state["levels"].values() if info.get("order_id")
    )

    # 9. Save state + heartbeat
    grid_state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_grid_state(grid_state)

    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(json.dumps({
        "ts":       datetime.now(timezone.utc).isoformat(),
        "price":    price,
        "week_pnl": risk_state.get("weekly_pnl_gbp", 0),
        "regime":   regime,
    }))

    # 10. Emit Prometheus metrics (Skill 6)
    _emit_prometheus(price, risk_state, config, regime, active_orders, hmm_conf)

    print(f"[grid] Run complete. Week P&L: £{risk_state.get('weekly_pnl_gbp', 0):.2f} "
          f"| Active orders: {active_orders}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    run()
