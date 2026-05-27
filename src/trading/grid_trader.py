"""
Grid trader — the core bot, run every minute by crontab.

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
import tempfile
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
from trading.trade_logger import append as log_trade, read_all as read_all_trades
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
PORTFOLIO_FILE  = ROOT / "data"   / "portfolio.json"    # live balance + P&L snapshot
PAUSE_FLAG      = ROOT / "data"   / "paused.flag"        # Telegram controller
RECENTER_FLAG   = ROOT / "data"   / "force_recenter.flag" # Telegram controller
TREND_PAUSE_FLAG = ROOT / "data"  / "trend_pause.flag"   # regime_classifier: strong trend detected
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
        LOCK_FILE.unlink(missing_ok=True)

    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT | O_EXCL is atomic — raises FileExistsError if lock was created
        # by another process between the exists() check above and this open().
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    except FileExistsError:
        print("[grid] Lock file created by concurrent process — exiting.")
        return False


def release_lock() -> None:
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()


# ------------------------------------------------------------------ #
#  Config helpers                                                      #
# ------------------------------------------------------------------ #

def refresh_total_capital(
    cdx:        CDXClient,
    config:     dict,
    grid_state: dict,
    risk_state: dict,
) -> dict:
    """
    Size the bot off the live whole portfolio and snapshot live P&L.

    Every run, `total_capital` is set to the real crypto.com balance (free +
    funds locked in open orders) so grid notional, the kill-switch threshold,
    the viability check, and the optimizer all scale with the actual balance.
    The static £150 only ever survives as a first-run bootstrap when the API
    is unreachable.

    Also writes data/portfolio.json — the single source of truth that the
    daily reporter and Telegram /status read (no extra live API calls there).

    Skips the capital update if the API call fails (last value is kept) or if
    the live value is implausibly small (<£10), guarding against a
    partially-settled balance snapshot returning near-zero.
    """
    gbp_usd = config.get("gbp_usd_rate", 1.27)
    try:
        portfolio = cdx.get_portfolio_value_gbp(gbp_usd_rate=gbp_usd)
    except Exception as e:
        print(f"[grid] Portfolio fetch failed: {e} — using cached total_capital={config.get('total_capital')}")
        return config

    live_gbp = portfolio["total_gbp"]
    if live_gbp < 10:
        print(f"[grid] Live portfolio £{live_gbp:.2f} — suspiciously low, keeping cached value")
        return config

    prev = config.get("total_capital", 0)
    config["total_capital"] = live_gbp
    if abs(live_gbp - prev) > 0.50:
        print(
            f"[grid] Portfolio updated: £{prev:.2f} → £{live_gbp:.2f} "
            f"(USDT={portfolio['usdt_total']:.2f}, "
            f"BTC={portfolio['btc_total']:.6f} @ ${portfolio['btc_price_usdt']:,.0f})"
        )
        # Persist so risk_manager, optimizer, and daily_reporter all see the same value.
        if CONFIG_FILE.exists():
            merged = json.loads(CONFIG_FILE.read_text())
            merged["total_capital"] = live_gbp
            _atomic_write(CONFIG_FILE, json.dumps(merged, indent=2))

    _write_portfolio_snapshot(portfolio, config, grid_state, risk_state)
    return config


def _write_portfolio_snapshot(
    portfolio:  dict,
    config:     dict,
    grid_state: dict,
    risk_state: dict,
) -> None:
    """
    Compute live P&L and persist data/portfolio.json every run.

    P&L views:
      - unrealised : (price − avg cost of held BTC) × BTC held, GBP
      - realised   : this week (risk_manager) + all-time (whole trade ledger)
      - since tracking : total now − baseline auto-snapshotted on first run
    """
    gbp_usd   = config.get("gbp_usd_rate", 1.27) or 1.27
    btc_price = portfolio.get("btc_price_usdt", 0.0)
    btc_total = portfolio.get("btc_total", 0.0)
    total_gbp = portfolio.get("total_gbp", 0.0)

    # Average cost of held BTC = mean of outstanding (unpaired) BUY fill prices.
    # buy_prices_queue stores prices only; per-level qty is near-equal so a
    # simple mean is a fair display approximation of cost basis.
    buy_q       = grid_state.get("buy_prices_queue") or []
    avg_cost    = (sum(buy_q) / len(buy_q)) if buy_q else None
    unrealised  = (
        (btc_price - avg_cost) * btc_total / gbp_usd
        if (avg_cost and btc_total > 0) else None
    )

    realised_week    = float(risk_state.get("weekly_pnl_gbp", 0.0))
    try:
        realised_alltime = sum(float(t.get("net_gbp", 0.0)) for t in read_all_trades())
    except Exception:
        realised_alltime = 0.0

    # Baseline: auto-snapshot the whole-portfolio value on the first run that
    # sees a valid balance. Labelled "since tracking started" — no deposit
    # figure is required from the user.
    baseline = None
    if PORTFOLIO_FILE.exists():
        try:
            baseline = json.loads(PORTFOLIO_FILE.read_text()).get("baseline_gbp")
        except Exception:
            baseline = None
    if baseline is None:
        baseline = total_gbp

    snapshot = {
        "total_gbp":            round(total_gbp, 2),
        "usdt_total":           portfolio.get("usdt_total", 0.0),
        "btc_total":            btc_total,
        "btc_value_gbp":        portfolio.get("btc_value_gbp", 0.0),
        "btc_price_usdt":       btc_price,
        "avg_cost_btc":         round(avg_cost, 2) if avg_cost else None,
        "unrealised_gbp":       round(unrealised, 2) if unrealised is not None else None,
        "realised_week_gbp":    round(realised_week, 2),
        "realised_alltime_gbp": round(realised_alltime, 2),
        "baseline_gbp":         round(baseline, 2),
        "since_tracking_gbp":   round(total_gbp - baseline, 2),
        "updated_at":           datetime.now(timezone.utc).isoformat(),
    }
    try:
        _atomic_write(PORTFOLIO_FILE, json.dumps(snapshot, indent=2))
    except Exception as e:
        print(f"[grid] Failed to write portfolio snapshot: {e}")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text())
    else:
        config = {
            "instrument":    "BTC_USDT",
            "spacing_pct":   1.0,
            "range_pct":     5.0,
            "levels":        6,
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


def load_grid_state() -> dict:
    if GRID_STATE_FILE.exists():
        try:
            return json.loads(GRID_STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "levels":           {},
        "calibration_price": None,
        "last_run":         None,
        "buy_prices_queue": [],   # FIFO queue of BUY fill prices for SELL P&L pairing
    }


def save_grid_state(state: dict) -> None:
    _atomic_write(GRID_STATE_FILE, json.dumps(state, indent=2))


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

_REGIME_GRID_SKEW = {
    # (buy_levels_fraction, sell_levels_fraction)
    # trending_up: more buys to catch dips during uptrend, fewer sells (they fill fast)
    "trending_up": 0.60,
    # trending_dn: more sells to capture bounces, fewer buys (don't catch falling knives)
    "trending_dn": 0.40,
    # ranging / volatile: symmetric
    "ranging":     0.50,
    "volatile":    0.50,
}


def build_grid(
    center_price: float,
    config:       dict,
    regime:       str = "ranging",
    stance:       str = "NEUTRAL",
) -> list[dict]:
    """
    Generate grid levels around center_price.

    buy_fraction is regime-aware: trending_up skews 60% buy / 40% sell,
    trending_dn skews 40% buy / 60% sell, others are symmetric 50/50.
    This reduces the wasted half-grid that never fills during trends.

    The AI strategy stance modulates this within safe bounds:
      WITH_TREND / NEUTRAL → keep the regime skew (today's behaviour)
      AGAINST_TREND        → force a symmetric grid (fade the range)
    STAND_ASIDE is handled upstream by the trend-pause flag (no grid built).

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

    if stance == "AGAINST_TREND":
        buy_frac = 0.50
    else:
        buy_frac = _REGIME_GRID_SKEW.get(regime, 0.50)
    n_buys    = round(levels * buy_frac)
    n_sells   = levels - n_buys
    # Ensure at least 1 level on each side
    n_buys    = max(1, min(n_buys,  levels - 1))
    n_sells   = max(1, levels - n_buys)

    if buy_frac != 0.50:
        print(f"[grid] Asymmetric grid ({regime}): {n_buys} buys / {n_sells} sells")

    result = []
    # Buy levels: below center (negative offsets, indices 0..n_buys-1)
    for i in range(n_buys):
        offset = -(n_buys - i) * spacing_pct   # e.g. -3s, -2s, -1s for n_buys=3
        price  = center_price * (1 + offset)
        qty    = per_level_usdt / price
        if qty < MIN_QTY_BTC:
            qty = MIN_QTY_BTC
        result.append({"level": i, "price": round(price, 2), "side": "BUY", "qty": round(qty, 6)})

    # Sell levels: at and above center (non-negative offsets, indices n_buys..levels-1)
    for j in range(n_sells):
        offset = j * spacing_pct               # e.g. 0, +1s, +2s for n_sells=3
        price  = center_price * (1 + offset)
        qty    = per_level_usdt / price
        if qty < MIN_QTY_BTC:
            qty = MIN_QTY_BTC
        result.append({"level": n_buys + j, "price": round(price, 2), "side": "SELL", "qty": round(qty, 6)})

    return result


# ------------------------------------------------------------------ #
#  Order placement                                                     #
# ------------------------------------------------------------------ #

def place_grid(
    cdx: CDXClient,
    levels: list[dict],
    grid_state: dict,
    best_bid: float = 0.0,
    best_ask: float = 0.0,
) -> None:
    """
    Place limit orders for each grid level. Skips already-live levels.

    Pre-placement spread check: a POST_ONLY BUY at price >= best_ask would be
    rejected by the exchange as a taker order, and a SELL at price <= best_bid
    likewise. We skip such levels and log clearly rather than burning an API
    call that will fail silently.
    """
    for lvl in levels:
        idx  = str(lvl["level"])
        side = lvl["side"]
        px   = lvl["price"]

        if idx in grid_state["levels"] and grid_state["levels"][idx].get("order_id"):
            continue

        # Spread-crossing guard.
        if best_bid and best_ask:
            # Full guard: skip any order that would immediately cross the spread.
            if side == "BUY" and px >= best_ask:
                print(f"[grid] Level {idx}: BUY @ {px:.2f} would cross ask {best_ask:.2f} "
                      f"— skipping (POST_ONLY would reject)")
                continue
            if side == "SELL" and px <= best_bid:
                print(f"[grid] Level {idx}: SELL @ {px:.2f} would cross bid {best_bid:.2f} "
                      f"— skipping (POST_ONLY would reject)")
                continue
        else:
            # No live book data — apply a conservative sanity check using the
            # calibration_price so we don't place wildly off-market orders.
            # (The exchange will still reject POST_ONLY crossings, but this
            # avoids wasting API quota on orders that are clearly misplaced.)
            cal = grid_state.get("calibration_price", 0)
            if cal > 0:
                if side == "BUY" and px >= cal * 1.001:
                    print(f"[grid] Level {idx}: BUY @ {px:.2f} above calibration {cal:.2f} "
                          f"with no book data — skipping")
                    continue
                if side == "SELL" and px <= cal * 0.999:
                    print(f"[grid] Level {idx}: SELL @ {px:.2f} below calibration {cal:.2f} "
                          f"with no book data — skipping")
                    continue

        try:
            order_id = cdx.place_limit_order(INSTRUMENT, side, px, lvl["qty"])
            grid_state["levels"][idx] = {
                "order_id":  order_id,
                "side":      side,
                "price":     px,
                "qty":       lvl["qty"],
                "placed_at": datetime.now(timezone.utc).isoformat(),
                "buy_price": px if side == "BUY" else None,
            }
            print(f"[grid] Placed {side} {lvl['qty']:.6f} BTC @ {px:.2f} (level {idx})")
        except (CDXError, Exception) as e:
            print(f"[grid] Failed to place level {idx}: {e}")


# ------------------------------------------------------------------ #
#  Open-order audit                                                    #
# ------------------------------------------------------------------ #

def audit_open_orders(cdx: CDXClient, grid_state: dict) -> None:
    """
    Reconcile grid_state against what the exchange actually has open.

    Ghost orders  — grid_state says an order is live but the exchange has
                    silently cancelled/rejected it.  The slot stays "occupied"
                    so place_grid() never re-fills it.  We clear these slots.

    Orphan orders — the exchange has an open order not tracked in grid_state
                    (crash remnant, manual trade, previous run).  We cancel
                    them to keep the grid clean and margin accurate.
    """
    try:
        open_orders = cdx.get_open_orders(INSTRUMENT)
    except Exception as e:
        print(f"[grid] audit: get_open_orders failed: {e} — skipping audit")
        return

    live_ids    = {o["order_id"] for o in open_orders}
    tracked_ids = {
        info["order_id"]: idx
        for idx, info in grid_state["levels"].items()
        if info.get("order_id")
    }

    # 1. Ghost detection: tracked in grid_state but no longer open on exchange.
    #
    # Two-phase to prevent a race condition: if an order fills between the
    # get_order_history() call in detect_fills() and the get_open_orders() call
    # here, it won't be in either snapshot. Clearing it immediately would silently
    # drop the fill and corrupt the buy_prices_queue.
    #
    # Phase 1: mark as pending_ghost. detect_fills() on the next run will find
    #          the order in history as FILLED and process it properly.
    # Phase 2: if still absent on the following run, it is a true ghost — clear it.
    for oid, idx in tracked_ids.items():
        if oid not in live_ids:
            info = grid_state["levels"].get(idx, {})
            if info.get("pending_ghost"):
                print(
                    f"[grid] Confirmed ghost {oid} (level {idx} "
                    f"{info.get('side','')} @ {info.get('price', 0):.2f}) "
                    f"— absent two runs in a row, clearing slot"
                )
                grid_state["levels"][idx] = {}
            else:
                print(
                    f"[grid] Potential ghost {oid} (level {idx} "
                    f"{info.get('side','')} @ {info.get('price', 0):.2f}) "
                    f"— not in open orders, will confirm next run"
                )
                grid_state["levels"][idx]["pending_ghost"] = True

    # 2. Orphan detection: live on exchange but not tracked in grid_state
    for o in open_orders:
        oid = o["order_id"]
        if oid not in tracked_ids:
            print(
                f"[grid] Orphan order {oid} "
                f"({o.get('side','')} @ {o.get('price', 0):.2f}) "
                f"— not in grid_state, cancelling"
            )
            try:
                cdx.cancel_order(INSTRUMENT, oid)
            except Exception as e:
                print(f"[grid] Failed to cancel orphan {oid}: {e}")


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
        history = cdx.get_order_history(INSTRUMENT, limit=200)
    except Exception as e:
        print(f"[grid] Failed to fetch order history: {e} — skipping fill detection")
        return
    # Include CANCELED orders that had a partial fill — the filled portion represents
    # real BTC exchanged and must be recorded to keep cost basis accurate.
    filled = {
        o["order_id"]: o
        for o in history
        if o.get("status") == "FILLED"
        or (o.get("status") == "CANCELED" and float(o.get("cumulative_quantity", 0)) > 0)
    }
    week_start = risk_state.get("week_start", "")

    # FIFO queue of BUY fill prices — persisted in grid_state across runs so
    # each SELL can be paired with the oldest outstanding BUY for accurate P&L.
    buy_q = grid_state.setdefault("buy_prices_queue", [])

    for idx, info in list(grid_state["levels"].items()):
        oid = info.get("order_id")
        if not oid or oid not in filled:
            continue

        order  = filled[oid]
        side   = info["side"]
        price  = float(order.get("avg_price", info["price"]))
        qty    = float(order.get("cumulative_quantity", info["qty"]))
        fee    = float(order.get("cumulative_fee", 0))

        if side == "BUY":
            # Record actual fill price so the next SELL can compute correct gross profit.
            buy_q.append(price)
            buy_px = None
        else:
            # Pop the oldest BUY price (FIFO). Fall back to calibration_price when the
            # queue is empty (e.g. SELL of BTC held before the bot started).
            if buy_q:
                buy_px = buy_q.pop(0)
            else:
                buy_px = grid_state.get("calibration_price")
                if buy_px:
                    print(f"[grid] SELL @ {price:.2f}: buy_prices_queue empty — "
                          f"using calibration_price {buy_px:.2f} as cost basis")

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

        # Count both BUY fees (negative) and SELL profit (positive) in weekly P&L.
        risk_state = record_trade(entry["net_gbp"])
        if side == "SELL":
            print(f"[grid] SELL filled @ {price:.2f} | net P&L: £{entry['net_gbp']:.4f}")
        else:
            print(f"[grid] BUY filled @ {price:.2f} | fee: £{entry['net_gbp']:.4f}")

        grid_state["levels"][idx] = {}

    record_warning(risk_state)


# ------------------------------------------------------------------ #
#  Stale order management                                              #
# ------------------------------------------------------------------ #

STALE_ORDER_AGE_HOURS = 72   # cancel orders older than this (raised from 48 to reduce churn)

def cancel_stale_orders(
    cdx:           CDXClient,
    grid_state:    dict,
    current_price: float,
    config:        dict,
) -> int:
    """
    Cancel open orders that are both old and far from the current market price.
    Returns the number of orders cancelled so place_grid() can refill the slots.

    Distance threshold uses range_pct (the actual recenter band) rather than
    a multiple of spacing — prevents over-cancellation in low-volatility chop
    where a SELL at +3% can sit legitimately for several days.
    """
    # Stale if order is outside the recenter band — at that point the grid will
    # recenter anyway on the next qualifying move, so cancel proactively.
    stale_distance = config.get("range_pct", 5.0) / 100
    cancelled      = 0

    for idx, info in list(grid_state["levels"].items()):
        oid = info.get("order_id")
        if not oid:
            continue
        placed_str = info.get("placed_at")
        if not placed_str:
            continue
        try:
            age_h        = (datetime.now(timezone.utc) - datetime.fromisoformat(placed_str)).total_seconds() / 3600
            order_price  = float(info.get("price", 0))
            distance_pct = abs(current_price - order_price) / current_price if order_price else 0

            if age_h >= STALE_ORDER_AGE_HOURS and distance_pct >= stale_distance:
                cdx.cancel_order(INSTRUMENT, oid)
                grid_state["levels"][idx] = {}
                print(
                    f"[grid] Stale order cancelled: {oid} level {idx} "
                    f"{info.get('side','')} @ {order_price:,.2f} "
                    f"({age_h:.0f}h old, {distance_pct*100:.1f}% from market)"
                )
                cancelled += 1
        except Exception as e:
            print(f"[grid] Failed to cancel stale order {oid}: {e}")

    if cancelled:
        print(f"[grid] {cancelled} stale order(s) cleared — slots will be refilled at current grid geometry")
    return cancelled


# ------------------------------------------------------------------ #
#  Range-break check                                                   #
# ------------------------------------------------------------------ #

def needs_recalibration(current_price: float, grid_state: dict, config: dict) -> bool:
    cal_price = grid_state.get("calibration_price")
    if cal_price is None or cal_price <= 0:
        return True
    move_pct   = abs(current_price - cal_price) / cal_price * 100
    # range_pct is the regime-aware recenter threshold — tight in volatile markets,
    # wider in trending ones so the bot doesn't churn the grid on every swing.
    threshold  = config.get("range_pct", 5.0)
    return move_pct > threshold


def cancel_all_and_clear(cdx: CDXClient, grid_state: dict) -> bool:
    """
    Cancel all open orders and clear the in-memory grid state.
    Returns True on success, False if the exchange rejected the cancellation.

    On failure: the old orders are still live — do NOT update calibration_price
    or place new orders, or both grids will coexist on the exchange simultaneously.
    """
    try:
        cdx.cancel_all_orders(INSTRUMENT)
    except CDXError as e:
        msg = (
            f"⚠️ *Recenter aborted — cancel_all_orders failed*\n"
            f"Error: {e}\n"
            f"Existing grid preserved. Will retry next run."
        )
        print(f"[grid] {msg}")
        _send_telegram_alert(msg)
        return False
    grid_state["levels"] = {}
    return True


def _handle_trend_pause_orders(regime: str) -> None:
    """
    Cancel the dangerous-direction orders when a trend pause is active.

    trending_dn → cancel open BUY orders (buying into a falling market causes losses)
    trending_up → cancel open SELL orders (selling early exits BTC before the top)

    Idempotent: fetches live open orders each call, so repeated calls during a
    multi-minute pause safely no-op once the relevant side is already gone.
    """
    if regime not in ("trending_dn", "trending_up"):
        return

    dangerous_side = "BUY" if regime == "trending_dn" else "SELL"
    direction_label = "down" if regime == "trending_dn" else "up"

    try:
        cdx = CDXClient()
        open_orders = cdx.get_open_orders(INSTRUMENT)
    except Exception as e:
        print(f"[grid] trend-pause cancel: could not fetch open orders: {e}")
        return

    to_cancel = [o for o in open_orders if o.get("side") == dangerous_side]
    if not to_cancel:
        return

    grid_state  = load_grid_state()
    cancel_ids  = {o["order_id"] for o in to_cancel}
    failed      = []

    for o in to_cancel:
        try:
            cdx.cancel_order(INSTRUMENT, o["order_id"])
        except Exception as e:
            print(f"[grid] trend-pause: failed to cancel {o['order_id']}: {e}")
            failed.append(o["order_id"])

    # Remove successfully cancelled orders from grid state
    grid_state["levels"] = {
        k: v for k, v in grid_state["levels"].items()
        if v.get("order_id") not in (cancel_ids - set(failed))
    }
    save_grid_state(grid_state)

    cancelled_count = len(to_cancel) - len(failed)
    msg = (
        f"📉 *Trend pause ({direction_label})* — cancelled {cancelled_count} "
        f"{dangerous_side} order(s) to avoid directional risk."
    )
    if failed:
        msg += f"\n⚠️ {len(failed)} order(s) could not be cancelled — check exchange."
    print(f"[grid] {msg}")
    _send_telegram_alert(msg)



def run() -> None:
    if not acquire_lock():
        return
    try:
        _run()
    finally:
        release_lock()


def _check_stale_heartbeat() -> None:
    """Alert if the bot was down for more than 30 minutes (VM reboot, OOM, etc.)."""
    if not HEARTBEAT_FILE.exists():
        return
    try:
        hb      = json.loads(HEARTBEAT_FILE.read_text())
        hb_dt   = datetime.fromisoformat(hb["ts"])
        age_min = (datetime.now(timezone.utc) - hb_dt).total_seconds() / 60
        if age_min <= 30:
            return

        # Gather diagnostics so the alert is self-contained for review
        price_str  = f"${hb.get('price', 0):,.0f}" if hb.get("price") else "unknown"
        pnl_str    = f"£{hb.get('week_pnl', 0):.2f}"
        regime_str = hb.get("regime", "unknown")
        lock_str   = "yes — possible hung run" if LOCK_FILE.exists() else "no"

        # Last error line from the log (best-effort)
        last_err = "none found"
        try:
            log_path = ROOT / "logs" / "grid_trader.log"
            if log_path.exists():
                lines = log_path.read_text().splitlines()
                errors = [l for l in lines if "ERROR" in l or "CRITICAL" in l or "Traceback" in l]
                last_err = errors[-1] if errors else "none found"
        except Exception:
            pass

        msg = (
            f"⚠️ *Grid bot resumed after gap*\n"
            f"Gap: {age_min:.0f} min (last heartbeat {hb_dt.strftime('%H:%M UTC')})\n"
            f"Last known: price={price_str} | regime={regime_str} | week P&L={pnl_str}\n"
            f"Lock file present: {lock_str}\n"
            f"Last log error: `{last_err[-120:]}`\n"
            f"_Paste this to Claude Code to diagnose._"
        )
        print(f"[grid] {msg}")
        _send_telegram_alert(msg)
        # Stamp now so this alert only fires once per gap, even on early exits.
        hb["ts"] = datetime.now(timezone.utc).isoformat()
        HEARTBEAT_FILE.write_text(json.dumps(hb))
    except Exception:
        pass


def _run() -> None:
    _check_stale_heartbeat()

    # Stamp liveness immediately so the stale-heartbeat alert doesn't fire during
    # intentional pauses (trend pause, kill switch, Telegram pause). Only a real
    # crash or VM outage — where this line never runs — will trigger the alert.
    try:
        if HEARTBEAT_FILE.exists():
            hb = json.loads(HEARTBEAT_FILE.read_text())
        else:
            hb = {}
        hb["ts"] = datetime.now(timezone.utc).isoformat()
        HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(json.dumps(hb))
    except Exception:
        pass

    # 1. Kill switch check
    if is_kill_switch_active():
        print("[grid] Kill switch is active — bot paused until Monday. Exiting.")
        return

    # 2. Pause flag check (set by Telegram controller — Skill 3)
    if PAUSE_FLAG.exists():
        print("[grid] Pause flag active — bot paused by Telegram command. Exiting.")
        return

    # 2b. Trend pause check (set by regime_classifier when strong trend detected)
    # Cancel the dangerous-direction orders (buys on dn-trend, sells on up-trend)
    # to avoid directional risk. Idempotent — safe to call every minute.
    if TREND_PAUSE_FLAG.exists():
        regime_data = load_regime()
        _handle_trend_pause_orders(regime_data.get("regime", ""))
        print("[grid] Trend pause active — standing aside. Exiting.")
        return

    risk_state  = get_risk_state()
    config      = load_config()
    regime_data = load_regime()
    regime      = regime_data.get("regime", "ranging")
    hmm_conf    = float(regime_data.get("hmm_confidence", 0.0))
    stance      = regime_data.get("stance", "NEUTRAL")

    grid_state = load_grid_state()
    cdx        = CDXClient()

    # 3. Refresh total_capital from live portfolio (replaces hardcoded £150)
    #    and snapshot live P&L to data/portfolio.json.
    config = refresh_total_capital(cdx, config, grid_state, risk_state)

    # 4. Apply regime-recommended params if HMM is confident (Skill 2 fix)
    config = apply_regime_params(config, regime_data)

    # 5. CDaR-adjusted capital_pct (Skill 4)
    config["capital_pct"] = get_dynamic_capital_pct(config["capital_pct"])

    # 6. Current price — prefer order book mid-price; fall back to last trade
    best_bid = best_ask = 0.0
    try:
        book     = cdx.get_order_book(INSTRUMENT, depth=5)
        best_bid = book["best_bid"]
        best_ask = book["best_ask"]
        mid      = book["mid_price"]
        spread   = book["spread"]
        if mid > 0:
            price = mid
            print(f"[grid] Order book: bid={best_bid:,.2f} ask={best_ask:,.2f} "
                  f"spread={spread:.2f} mid={mid:,.2f}")
        else:
            raise CDXError("Order book returned zero mid-price")
    except (CDXError, Exception) as e:
        print(f"[grid] get_order_book failed: {e} — falling back to ticker")
        try:
            ticker   = cdx.get_ticker(INSTRUMENT)
            price    = ticker["price"]
            best_bid = ticker.get("bid", 0.0)
            best_ask = ticker.get("ask", 0.0)
        except (CDXError, httpx.TimeoutException, Exception) as e2:
            print(f"[grid] get_ticker also failed: {e2} — aborting run.")
            return

    if price <= 0:
        print("[grid] Price is 0 or negative — aborting run.")
        return

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

    print(f"[grid] BTC/USDT: {price:,.2f} | Regime: {regime} (HMM conf={hmm_conf:.2f}) | Stance: {stance}")

    # 6. Detect fills from previous orders
    detect_fills(cdx, grid_state, config, regime, risk_state)

    # 6b. Audit open orders — clear ghost slots, cancel orphan orders
    audit_open_orders(cdx, grid_state)

    # 6c. Cancel orders that are stale (old AND far from market price)
    cancel_stale_orders(cdx, grid_state, price, config)

    # Re-check kill switch — a fill may have triggered it
    if is_kill_switch_active():
        print("[grid] Kill switch triggered by fill — cancelling all orders.")
        cancel_all_and_clear(cdx, grid_state)  # alert already sent inside if cancel fails
        save_grid_state(grid_state)
        return

    # Priority 3: Monday morning AI briefing (fires once on weekly reset)
    # NOTE: by the time we reach this check, get_risk_state() has already reset
    # weekly_pnl_gbp to 0.0. We use the snapshot stored in grid_state at the
    # END of the previous week's final run (see bottom of _run()).
    last_seen_week = grid_state.get("last_seen_week_start")
    current_week   = risk_state.get("week_start", "")
    if current_week and last_seen_week != current_week:
        grid_state["last_seen_week_start"] = current_week
        try:
            send_monday_briefing(
                last_week_pnl    = float(grid_state.get("prev_week_pnl_gbp", 0)),
                last_week_trades = int(grid_state.get("prev_week_trades", 0)),
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
        if not cancel_all_and_clear(cdx, grid_state):
            # Exchange rejected cancel — abort recenter, keep existing grid intact.
            do_recenter = False
        else:
            grid_state["calibration_price"] = price
            grid_state.pop("outside_since", None)
            # Persist the clean cancelled state NOW, before placing new orders.
            # If the process is killed between here and place_grid(), next startup
            # finds empty levels — new orders will be detected as orphans and
            # removed, then this run's calibration_price triggers a fresh recenter.
            save_grid_state(grid_state)

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
    # Always use calibration_price as the grid centre so replacement orders after
    # fills are geometrically consistent with the existing live orders.
    # Using live price here causes grid drift: replacements shift reference points
    # every run, eventually producing BUY orders above live SELL orders.
    # calibration_price is always set — either from disk state or from the
    # recenter that just ran above (which sets it to the live price).
    grid_center = grid_state.get("calibration_price") or price
    levels = build_grid(grid_center, config, regime=regime, stance=stance)
    place_grid(cdx, levels, grid_state, best_bid=best_bid, best_ask=best_ask)

    # Count active orders for Prometheus
    active_orders = sum(
        1 for info in grid_state["levels"].values() if info.get("order_id")
    )

    if active_orders == 0:
        msg = "[grid] WARNING: zero active orders after placement — grid is empty, check logs"
        print(msg)
        _send_telegram_alert(msg)

    # 9. Save state + heartbeat
    grid_state["last_run"] = datetime.now(timezone.utc).isoformat()
    # Snapshot current P&L so the Monday briefing has last week's real numbers
    # even after get_risk_state() resets the weekly counter on Monday.
    grid_state["prev_week_pnl_gbp"] = risk_state.get("weekly_pnl_gbp", 0)
    grid_state["prev_week_trades"]  = risk_state.get("trades_this_week", 0)
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
