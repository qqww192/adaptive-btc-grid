"""
fetch_and_sync.py
Fetch T212 portfolio + yfinance prices → sync to Google Sheet.
No AI, no Gemini. Just data.

Usage: cd src && python fetch_and_sync.py
"""

import sys
import logging
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    log.info("=== Fetch & Sync (no AI) ===")

    # ── Step 1: Connect to sheet ─────────────────────────────────────────
    from sheets import SheetManager
    sheet = SheetManager()
    log.info("Sheet: %s", sheet.url)

    # ── Step 2: Fetch T212 portfolio ─────────────────────────────────────
    log.info("Fetching T212 portfolio...")
    from fetch_portfolio import fetch_all_positions
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions returned.")
        return
    log.info("%d positions fetched.", len(positions))
    for p in positions[:10]:
        log.info("  %-10s qty=%.2f  price=%.2f  P/L=%.2f",
                 p.get("ticker", "?"), p.get("quantity", 0),
                 p.get("currentPrice", 0), p.get("ppl", 0))
    if len(positions) > 10:
        log.info("  ... and %d more", len(positions) - 10)

    # ── Step 3: Get live prices from yfinance ────────────────────────────
    log.info("Fetching live prices from yfinance...")
    from market_data import get_batch_prices
    symbols = [p.get("ticker", "") for p in positions if p.get("ticker")]
    live_prices = get_batch_prices(symbols)
    log.info("Live prices fetched for %d symbols.", len(live_prices))

    # ── Step 4: Sync to Portfolio tab ────────────────────────────────────
    log.info("Syncing to Portfolio tab...")
    sheet.sync_portfolio(positions, prices=live_prices)
    log.info("Portfolio synced.")

    # ── Step 5: Market scorecard (yfinance only) ─────────────────────────
    log.info("Building market scorecard...")
    from market_data import get_market_scorecard
    scorecard = get_market_scorecard()
    sheet.write_market_overview(scorecard)
    log.info("Market Overview updated (%d rows).", len(scorecard))

    # ── Step 6: Signal metrics (yfinance only) ───────────────────────────
    log.info("Computing signal metrics...")
    from market_data import get_signal_metrics
    signals = get_signal_metrics()
    sheet.write_signals(signals)
    for s in signals:
        log.info("  %-25s %10s  [%s]", s["name"], s["value"], s["signal"])

    # ── Step 7: Evaluate alerts ──────────────────────────────────────────
    from market_data import evaluate_alert
    alerts = sheet.get_alerts()
    if alerts:
        log.info("Evaluating %d alerts...", len(alerts))
        for alert in alerts:
            try:
                current = evaluate_alert(alert["symbol"], alert["metric"])
                threshold = float(alert["threshold"])
                condition = alert["condition"].strip().lower()
                if condition in ("above", ">", ">="):
                    triggered = current >= threshold
                elif condition in ("below", "<", "<="):
                    triggered = current <= threshold
                else:
                    continue
                sheet.update_alert_status(alert["row_index"], current, triggered)
                status = "TRIGGERED" if triggered else "OK"
                log.info("  %s %s %s %s → %s (%.2f)",
                         alert["symbol"], alert["metric"], condition,
                         alert["threshold"], status, current)
            except Exception as e:
                log.error("  Alert %s failed: %s", alert["symbol"], e)
    else:
        log.info("No alerts configured.")

    log.info("=== Done — sheet updated (no AI used) ===")
    log.info("Sheet: %s", sheet.url)


if __name__ == "__main__":
    main()
