"""
fetch_and_sync.py
Fetch T212 portfolio + yfinance prices → sync to Google Sheet.
No AI, no Gemini. Just data.

Pipeline priority (same as main.py but without AI):
  1. Evaluate alerts → Telegram if triggered
  2. Market scorecard + signals → Signals tab
  3. T212 portfolio → Portfolio tab

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

    import telegram_notify

    high_risk_items: list[str] = []

    # ── Step 2: Evaluate alerts ──────────────────────────────────────────
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
                if triggered:
                    telegram_notify.notify_alert_triggered(alert, current)
                    high_risk_items.append(
                        f"Alert: {alert['symbol']} {alert['metric']} {condition} "
                        f"{alert['threshold']} (current: {current:.2f})"
                    )
            except Exception as e:
                log.error("  Alert %s failed: %s", alert["symbol"], e)
    else:
        log.info("No alerts configured.")

    # ── Step 3: Market scorecard → Signals tab ───────────────────────────
    log.info("Fetching market scorecard...")
    from market_data import get_market_scorecard
    scorecard = get_market_scorecard()
    sheet.write_market_overview(scorecard)
    log.info("Market scorecard written (%d rows).", len(scorecard))

    for entry in scorecard:
        if entry.get("name") == "Market Health" and entry.get("signal") in ("Danger", "Stressed"):
            msg = f"Market Health: {entry['value']}/100 ({entry['signal']})"
            high_risk_items.append(msg)
            telegram_notify.notify_high_risk("Market Health", msg)

    # ── Step 4: Signal metrics → Signals tab ─────────────────────────────
    log.info("Computing signal metrics...")
    from market_data import get_signal_metrics
    signals = get_signal_metrics()
    sheet.write_signals(signals, signal_type="Signal")
    for s in signals:
        log.info("  %-25s %10s  [%s]", s["name"], s["value"], s["signal"])

    for s in signals:
        if s["name"] == "Fear & Greed Score":
            try:
                fg_score = int(s["value"])
                if fg_score <= 20:
                    msg = f"Fear & Greed: {fg_score} ({s['signal']})"
                    high_risk_items.append(msg)
                    telegram_notify.notify_high_risk("Fear & Greed", msg)
            except (ValueError, TypeError):
                pass

    # ── Step 5: Fetch T212 portfolio ─────────────────────────────────────
    log.info("Fetching T212 portfolio...")
    from fetch_portfolio import fetch_all_positions, t212_to_yfinance
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions returned.")
    else:
        log.info("%d positions fetched.", len(positions))
        for p in positions[:10]:
            log.info("  %-10s qty=%.4f  avgPrice=%.2f  price=%.2f  P/L=%.2f",
                     p["ticker"], p["quantity"],
                     p["averagePrice"], p["currentPrice"],
                     p["ppl"])
        if len(positions) > 10:
            log.info("  ... and %d more", len(positions) - 10)

        # Get live prices from yfinance (map T212 tickers to yfinance format)
        log.info("Fetching live prices from yfinance...")
        from market_data import get_batch_prices
        t212_tickers = [p["ticker"] for p in positions if p.get("ticker")]
        yf_tickers = [t212_to_yfinance(t) for t in t212_tickers]
        log.info("  T212 tickers: %s", t212_tickers[:5])
        log.info("  yfinance tickers: %s", yf_tickers[:5])
        yf_prices = get_batch_prices(yf_tickers)
        live_prices = {}
        for t212_t, yf_t in zip(t212_tickers, yf_tickers):
            if yf_t in yf_prices and yf_prices[yf_t]:
                live_prices[t212_t] = yf_prices[yf_t]
        log.info("Live prices fetched for %d/%d symbols.", len(live_prices), len(t212_tickers))

        # Sync to Portfolio tab
        log.info("Syncing to Portfolio tab...")
        sheet.sync_portfolio(positions, prices=live_prices)
        log.info("Portfolio synced.")

    # ── Step 6: Telegram summary if high-risk ────────────────────────────
    if high_risk_items:
        summary_lines = "\n".join(f"- {item}" for item in high_risk_items)
        telegram_notify.notify_high_risk(
            "Fetch & Sync Risk Summary",
            f"{len(high_risk_items)} risk item(s):\n\n{summary_lines}",
        )

    log.info("=== Done — sheet updated (no AI used) ===")
    log.info("Sheet: %s", sheet.url)


if __name__ == "__main__":
    main()
