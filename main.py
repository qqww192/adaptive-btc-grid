"""
T212 Portfolio Checker — Main Orchestrator
Runs the full pipeline: fetch → analyse → report
"""

import os
import sys
import logging
from datetime import datetime
from dotenv import load_dotenv

from fetch_portfolio import fetch_all_positions
from analyse import analyse_portfolio
from report import write_report_to_drive

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()  # no-op in GitHub Actions (secrets injected as env vars)

    log.info("=== T212 Portfolio Checker starting ===")
    run_date = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ── Step 1: Fetch portfolio ───────────────────────────────────────────────
    log.info("Step 1 — Fetching portfolio from Trading 212...")
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions returned. Exiting.")
        sys.exit(0)
    log.info(f"  {len(positions)} positions fetched.")

    # ── Step 2: Analyse with Claude ──────────────────────────────────────────
    log.info("Step 2 — Analysing portfolio with Claude...")
    report_markdown = analyse_portfolio(positions, run_date)
    log.info("  Analysis complete.")

    # ── Step 3: Write to Google Drive ────────────────────────────────────────
    log.info("Step 3 — Writing report to Google Drive...")
    doc_url = write_report_to_drive(report_markdown, run_date)
    log.info(f"  Report written: {doc_url}")

    log.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()
