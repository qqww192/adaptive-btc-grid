"""
run_news_alert.py
Entry point for the daily news alert job.
Separate from main.py so the two pipelines can run on independent schedules.
"""

import os
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv

from fetch_portfolio import fetch_all_positions
from news_alert import scan_for_alerts, send_telegram_alerts, format_alert_for_doc
from report import write_alert_log_to_drive

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    log.info("=== Daily News Alert Scanner starting ===")
    scan_date = datetime.utcnow().strftime("%Y-%m-%d")

    # Step 1: get current holdings so Claude knows which ETFs to monitor
    log.info("Fetching current portfolio...")
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions found — nothing to monitor. Exiting.")
        sys.exit(0)
    log.info(f"  {len(positions)} positions loaded.")

    # Step 2: run the news scan
    log.info("Running news scan...")
    result = scan_for_alerts(positions, scan_date)
    tier1_count = len(result.get("tier1_alerts", []))
    tier2_count = len(result.get("tier2_log", []))
    log.info(f"  Tier 1 alerts: {tier1_count} | Tier 2 logged: {tier2_count}")
    log.info(f"  Market temperature: {result.get('overall_market_temperature', 'unknown')}")

    # Step 3: send Telegram — Tier 1 alerts or quiet-day summary
    log.info("Sending Telegram notification...")
    alert_sent = send_telegram_alerts(result, scan_date)

    # Step 4: always log full results to Google Doc (audit trail)
    log.info("Logging scan results to Google Drive...")
    doc_markdown = format_alert_for_doc(result)
    write_alert_log_to_drive(doc_markdown, scan_date)

    log.info(f"=== Scan complete. Tier 1 alert sent: {alert_sent} ===")


if __name__ == "__main__":
    main()
