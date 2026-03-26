"""
test_setup.py
Interactive setup script to test Sheet access and T212 portfolio sync.
Skips Gemini analysis — just validates Google Sheets + T212 connections.

Usage:
  1. Create a .env file with your secrets (see below)
  2. Run: python src/test_setup.py

Required .env variables:
  T212_API_KEY=...
  T212_SECRET_KEY=...
  GOOGLE_SA_JSON={"type":"service_account",...}
  GOOGLE_SHEET_ID=...          (ID of existing spreadsheet shared with SA)
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

    # ── Step 0: Diagnose service account ────────────────────────────────────
    import os, json
    sa_json = os.environ.get("GOOGLE_SA_JSON", "")
    if sa_json:
        try:
            sa = json.loads(sa_json)
            log.info("Service account email : %s", sa.get("client_email", "MISSING"))
            log.info("Project ID            : %s", sa.get("project_id", "MISSING"))
        except json.JSONDecodeError:
            log.error("GOOGLE_SA_JSON is not valid JSON!")
    else:
        log.error("GOOGLE_SA_JSON is empty!")

    log.info("GOOGLE_SHEET_ID       : %s", os.environ.get("GOOGLE_SHEET_ID", "(not set)"))

    # ── Step 0.5: Test raw Google API auth ───────────────────────────────
    log.info("")
    log.info("STEP 0.5: Testing Google API authentication...")
    log.info("-" * 40)
    try:
        from google.oauth2 import service_account as _sa
        from googleapiclient.discovery import build as _build

        _creds = _sa.Credentials.from_service_account_info(
            json.loads(sa_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        log.info("  Credentials created OK for: %s", _creds.service_account_email)

        sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            log.error("  GOOGLE_SHEET_ID not set. Set it to an existing spreadsheet ID.")
            sys.exit(1)

        _sheets = _build("sheets", "v4", credentials=_creds)
        ss = _sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
        log.info("  Sheets API: OK — can read '%s'", ss.get("properties", {}).get("title", "Untitled"))

    except Exception as e:
        log.error("AUTH TEST FAILED: %s", e)
        log.error("")
        log.error("Troubleshooting checklist:")
        log.error("  1. Is the Google Sheets API enabled for project '%s'?", sa.get("project_id", "?"))
        log.error("     → https://console.cloud.google.com/apis/library/sheets.googleapis.com")
        log.error("  2. Is the spreadsheet shared with the service account email as Editor?")
        log.error("  3. Was GOOGLE_SA_JSON pasted correctly? (multiline JSON can break in GitHub Secrets)")
        sys.exit(1)

    # ── Step 1: Test Google Sheets connection ──────────────────────────────
    log.info("")
    log.info("STEP 1: Connecting to Google Sheet...")
    log.info("-" * 40)
    try:
        from sheets import SheetManager
        sheet = SheetManager()
        log.info("SUCCESS — Sheet connected!")
        log.info("  Sheet URL: %s", sheet.url)
    except Exception as e:
        log.error("FAILED — Could not access Google Sheet: %s", e)
        log.error("Check your GOOGLE_SA_JSON and GOOGLE_SHEET_ID env vars.")
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
