"""
test_setup.py
Interactive setup script to test Sheet creation and T212 portfolio sync.
Skips Gemini analysis — just validates Google Sheets + T212 connections.

Usage:
  1. Create a .env file with your secrets (see below)
  2. Run: python src/test_setup.py

Required .env variables:
  T212_API_KEY=...
  T212_SECRET_KEY=...
  GOOGLE_SA_JSON={"type":"service_account",...}
  GOOGLE_DRIVE_FOLDER_ID=...   (optional — folder to create sheet in)
  GOOGLE_SHEET_ID=             (leave blank — will be created)
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

    log.info("=" * 60)
    log.info("T212 Portfolio Checker — Setup Test")
    log.info("=" * 60)

    # ── Step 1: Test Google Sheets connection & create sheet ───────────────
    log.info("")
    log.info("STEP 1: Creating Google Sheet...")
    log.info("-" * 40)
    try:
        from sheets import SheetManager
        sheet = SheetManager()
        log.info("SUCCESS — Sheet created/found!")
        log.info("  Sheet URL: %s", sheet.url)
        log.info("")
        log.info("  >>> Save this Sheet ID to your GitHub Secrets as GOOGLE_SHEET_ID:")
        log.info("  >>> %s", sheet.sheet_id)
        log.info("")
    except Exception as e:
        log.error("FAILED — Could not create/access Google Sheet: %s", e)
        log.error("Check your GOOGLE_SA_JSON and GOOGLE_DRIVE_FOLDER_ID env vars.")
        sys.exit(1)

    # ── Step 2: Fetch T212 portfolio and sync to sheet ─────────────────────
    log.info("STEP 2: Fetching Trading 212 portfolio...")
    log.info("-" * 40)
    try:
        from fetch_portfolio import fetch_all_positions
        positions = fetch_all_positions()
        if not positions:
            log.warning("No positions returned from Trading 212.")
            log.warning("This might be normal if your portfolio is empty.")
        else:
            log.info("SUCCESS — Fetched %d positions.", len(positions))
            # Show first 5
            for pos in positions[:5]:
                log.info(
                    "  %s: qty=%.2f, avg=%.2f, current=%.2f, P/L=%.2f",
                    pos.get("ticker", "?"),
                    pos.get("quantity", 0),
                    pos.get("averagePrice", 0),
                    pos.get("currentPrice", 0),
                    pos.get("ppl", 0),
                )
            if len(positions) > 5:
                log.info("  ... and %d more", len(positions) - 5)
    except Exception as e:
        log.error("FAILED — Could not fetch T212 portfolio: %s", e)
        log.error("Check your T212_API_KEY and T212_SECRET_KEY env vars.")
        sys.exit(1)

    # ── Step 3: Sync portfolio to sheet ────────────────────────────────────
    log.info("")
    log.info("STEP 3: Syncing portfolio to Google Sheet...")
    log.info("-" * 40)
    try:
        sheet.sync_portfolio(positions)
        log.info("SUCCESS — Portfolio synced to sheet!")
    except Exception as e:
        log.error("FAILED — Could not sync portfolio: %s", e)
        sys.exit(1)

    # ── Step 4: Verify sheet contents ──────────────────────────────────────
    log.info("")
    log.info("STEP 4: Verifying sheet contents...")
    log.info("-" * 40)
    try:
        portfolio = sheet.get_portfolio_for_analysis()
        log.info("Portfolio tab: %d stocks ready for analysis", len(portfolio))
        for stock in portfolio[:5]:
            log.info(
                "  %s (%s) — priority %d",
                stock["symbol"], stock["market"], stock["priority"],
            )
        if len(portfolio) > 5:
            log.info("  ... and %d more", len(portfolio) - 5)

        watchlist = sheet.get_watchlist()
        log.info("Watchlist tab: %d symbols", len(watchlist))

        log.info("Market Overview tab: ready (empty until first analysis run)")
    except Exception as e:
        log.error("FAILED — Could not read sheet: %s", e)
        sys.exit(1)

    # ── Summary ────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("ALL STEPS PASSED!")
    log.info("=" * 60)
    log.info("")
    log.info("Your sheet is ready: %s", sheet.url)
    log.info("")
    log.info("Next steps:")
    log.info("  1. Open the sheet URL above and verify the Portfolio tab")
    log.info("  2. Add the Sheet ID to GitHub Secrets as GOOGLE_SHEET_ID:")
    log.info("     %s", sheet.sheet_id)
    log.info("  3. (Optional) Add symbols to the Watchlist tab for research")
    log.info("  4. (Optional) Edit Priority column (1=high, 5=low, 0=skip)")
    log.info("  5. Gemini analysis will run on the next scheduled daily run")
    log.info("     or trigger manually via GitHub Actions workflow_dispatch")
    log.info("")
    log.info("Skipped: Gemini analysis (to preserve your daily API quota)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
