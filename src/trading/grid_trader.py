"""
Grid trader — the core bot, run every 5 minutes by crontab.

What it does each run
---------------------
1. Exit immediately if kill switch is active.
2. Load current grid params from config/grid_params.json.
3. Load current market regime from data/regime.json.
4. Fetch current BTC price.
5. Detect any orders that filled since the last run.
6. Log fills, update risk manager, place replacement orders.
7. Check if the grid band needs recentering (price moved > 3%).
8. Check kill switch threshold — halt and cancel if breached.

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
    get_state       as get_risk_state,
    is_kill_switch_active,
    record_trade,
    record_warning,
)
from trading.trade_logger import append as log_trade

# ------------------------------------------------------------------ #
#  Paths                                                               #
# ------------------------------------------------------------------ #
CONFIG_FILE     = ROOT / "config" / "grid_params.json"
REGIME_FILE     = ROOT / "data"   / "regime.json"
GRID_STATE_FILE = ROOT / "data"   / "grid_state.json"
LOCK_FILE       = ROOT / "data"   / "grid_trader.lock"
HEARTBEAT_FILE  = ROOT / "data"   / "last_heartbeat.json"

SAFE_BOUNDS = {
    "spacing_pct": (0.55, 3.0),
    "range_pct":   (2.0,  15.0),
    "levels":      (4,    20),
    "capital_pct": (0.40, 0.80),
    "kill_pct":    (0.05, 0.15),
}

INSTRUMENT = "BTC_USDT"


# ------------------------------------------------------------------ #
#  Lock / unlock                                                       #
# ------------------------------------------------------------------ #

def acquire_lock() -> bool:
    """Return False if another run is already in progress."""
    if LOCK_FILE.exists():
        # Stale lock? Kill if older than 4 minutes (one cron cycle)
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < 240:
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
    return {"regime": "ranging"}


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

MIN_QTY_BTC = 0.0001  # crypto.com minimum order size


def _send_telegram_alert(message: str) -> None:
    """Fire-and-forget Telegram alert. Used for critical config errors."""
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
    spacing_pct   = config["spacing_pct"] / 100
    levels        = config["levels"]
    capital_gbp   = config["total_capital"] * config["capital_pct"]
    gbp_usd       = config["gbp_usd_rate"]
    capital_usdt  = capital_gbp * gbp_usd
    per_level_usdt = capital_usdt / levels

    result = []
    half   = levels // 2
    for i in range(levels):
        offset = (i - half) * spacing_pct
        price  = center_price * (1 + offset)
        side   = "SELL" if i >= half else "BUY"
        qty = per_level_usdt / price
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
    """
    Place limit orders for each grid level.
    Skips levels that already have a live order in grid_state.
    """
    for lvl in levels:
        idx = str(lvl["level"])
        if idx in grid_state["levels"] and grid_state["levels"][idx].get("order_id"):
            continue  # already have an order here
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
    filled   = {o["order_id"]: o for o in history if o.get("status") == "FILLED"}

    week_start = risk_state.get("week_start", "")

    for idx, info in list(grid_state["levels"].items()):
        oid = info.get("order_id")
        if not oid or oid not in filled:
            continue

        order = filled[oid]
        side  = info["side"]
        price = float(order.get("avg_price", info["price"]))
        qty   = float(order.get("cumulative_quantity", info["qty"]))
        fee   = float(order.get("cumulative_fee", 0))

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

        # Clear the level so it gets re-placed
        grid_state["levels"][idx] = {}

    # Check warning threshold
    record_warning(risk_state)


# ------------------------------------------------------------------ #
#  Range-break check (15-minute logic embedded here)                   #
# ------------------------------------------------------------------ #

def needs_recalibration(current_price: float, grid_state: dict, config: dict) -> bool:
    """
    Return True if price has moved more than 3% from the last calibration
    centre, meaning the grid band needs recentering.
    """
    cal_price = grid_state.get("calibration_price")
    if cal_price is None or cal_price <= 0:
        return True
    move_pct = abs(current_price - cal_price) / cal_price * 100
    return move_pct > 3.0


def cancel_all_and_clear(cdx: CDXClient, grid_state: dict) -> None:
    """Cancel every open order and clear grid state."""
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

    risk_state  = get_risk_state()
    config      = load_config()
    grid_state  = load_grid_state()
    regime_data = load_regime()
    regime      = regime_data.get("regime", "ranging")

    cdx = CDXClient()

    # 2. Current price
    try:
        ticker = cdx.get_ticker(INSTRUMENT)
    except (CDXError, httpx.TimeoutException, Exception) as e:
        print(f"[grid] get_ticker failed: {e} — aborting run.")
        return

    price = ticker["price"]
    print(f"[grid] BTC/USDT: {price:,.2f} | Regime: {regime}")

    # 3. Detect fills from previous orders
    detect_fills(cdx, grid_state, config, regime, risk_state)

    # Re-check kill switch — a fill may have triggered it
    if is_kill_switch_active():
        print("[grid] Kill switch triggered by fill — cancelling all orders.")
        cancel_all_and_clear(cdx, grid_state)
        save_grid_state(grid_state)
        return

    # 4. Recalibrate if price has drifted outside the grid range
    if needs_recalibration(price, grid_state, config):
        print(f"[grid] Grid needs recentering around {price:,.2f}")
        cancel_all_and_clear(cdx, grid_state)
        grid_state["calibration_price"] = price

    # Capital sufficiency check — exit if capital is below minimum for all levels
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

    # 5. Build and place grid
    levels = build_grid(price, config)
    place_grid(cdx, levels, grid_state)

    # 6. Save state
    grid_state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_grid_state(grid_state)

    # 7. Heartbeat — lets daily_reporter detect if the bot has silently stopped
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(json.dumps({
        "ts":       datetime.now(timezone.utc).isoformat(),
        "price":    price,
        "week_pnl": risk_state.get("weekly_pnl_gbp", 0),
    }))

    print(f"[grid] Run complete. Week P&L: £{risk_state.get('weekly_pnl_gbp', 0):.2f}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    run()
