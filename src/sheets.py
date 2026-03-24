"""
sheets.py
Google Sheets integration for the T212 Portfolio Checker.

Manages a single spreadsheet with three tabs:
  - Portfolio:       auto-populated from T212, with configurable priority
  - Watchlist:       user-added symbols for research
  - Market Overview: daily macro/sector analysis

Authentication: Google Service Account via GOOGLE_SA_JSON env var.
Sheet ID: GOOGLE_SHEET_ID env var (created automatically on first run).
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID: str = os.environ.get("GOOGLE_SHEET_ID", "")
REPORT_FOLDER_ID: str = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

# Tab names
TAB_PORTFOLIO = "Portfolio"
TAB_WATCHLIST = "Watchlist"
TAB_MARKET = "Market Overview"

# Column headers for each tab
PORTFOLIO_HEADERS = [
    "Pie Name", "Market (US/UK)", "Symbol", "Quantity", "Avg Price",
    "Current Price", "P/L", "Analysis Result", "Priority (1-5)",
    "Last Updated", "Created Date",
]

WATCHLIST_HEADERS = [
    "Symbol", "Market (US/UK)", "Analysis Result", "Last Updated", "Created Date",
]

MARKET_HEADERS = [
    "Date", "Region", "Index/Sector", "Weight in Portfolio",
    "Momentum Analysis", "Last Updated",
]


def _get_credentials() -> service_account.Credentials:
    sa_json = os.environ.get("GOOGLE_SA_JSON", "")
    if not sa_json:
        raise EnvironmentError(
            "GOOGLE_SA_JSON environment variable is not set. "
            "See docs/setup.md for configuration."
        )
    sa_dict = json.loads(sa_json)
    return service_account.Credentials.from_service_account_info(sa_dict, scopes=SCOPES)


def _empty_trash(drive_service) -> None:
    """Attempt to empty the service account's Drive trash to reclaim quota."""
    try:
        drive_service.files().emptyTrash().execute()
        log.info("Emptied Drive trash to reclaim storage quota.")
    except HttpError as e:
        log.warning("Failed to empty Drive trash: %s", e)


def _find_existing_sheet(drive_service) -> Optional[str]:
    """Search for an existing T212 Portfolio Tracker sheet."""
    try:
        results = drive_service.files().list(
            q="name = 'T212 Portfolio Tracker' and mimeType = 'application/vnd.google-apps.spreadsheet' and trashed = false",
            fields="files(id)",
            pageSize=1,
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
    except HttpError as e:
        log.warning("Failed to search for existing sheet: %s", e)
    return None


def _get_or_create_sheet(sheets_service, drive_service) -> str:
    """Returns the spreadsheet ID, creating one if needed."""
    # 1. Try configured ID
    if SHEET_ID:
        try:
            sheets_service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
            return SHEET_ID
        except HttpError:
            log.warning("GOOGLE_SHEET_ID '%s' is not accessible. Will find or create.", SHEET_ID)

    # 2. Search for existing
    existing = _find_existing_sheet(drive_service)
    if existing:
        log.info("Found existing sheet %s — reusing.", existing)
        return existing

    # 3. Create new spreadsheet with three tabs
    log.info("Creating new T212 Portfolio Tracker spreadsheet.")
    body = {
        "properties": {"title": "T212 Portfolio Tracker"},
        "sheets": [
            {"properties": {"title": TAB_PORTFOLIO}},
            {"properties": {"title": TAB_WATCHLIST}},
            {"properties": {"title": TAB_MARKET}},
        ],
    }

    try:
        spreadsheet = sheets_service.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    except HttpError as exc:
        if exc.resp.status == 403 and "storageQuotaExceeded" in str(exc):
            log.warning("Storage quota exceeded — emptying trash and retrying.")
            _empty_trash(drive_service)
            spreadsheet = sheets_service.spreadsheets().create(body=body, fields="spreadsheetId").execute()
        else:
            raise

    sheet_id = spreadsheet["spreadsheetId"]

    # Move to folder if configured
    if REPORT_FOLDER_ID:
        try:
            drive_service.files().update(
                fileId=sheet_id,
                addParents=REPORT_FOLDER_ID,
                fields="id, parents",
            ).execute()
        except HttpError as e:
            log.warning("Could not move sheet to folder %s: %s", REPORT_FOLDER_ID, e)

    # Write headers
    sheets_service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"'{TAB_PORTFOLIO}'!A1", "values": [PORTFOLIO_HEADERS]},
                {"range": f"'{TAB_WATCHLIST}'!A1", "values": [WATCHLIST_HEADERS]},
                {"range": f"'{TAB_MARKET}'!A1", "values": [MARKET_HEADERS]},
            ],
        },
    ).execute()

    log.info(
        "\n%s\nNew Google Sheet created!\n"
        "Sheet ID: %s\n"
        "Set this as GOOGLE_SHEET_ID in your GitHub Secrets.\n%s",
        "=" * 60, sheet_id, "=" * 60,
    )
    return sheet_id


class SheetManager:
    """High-level interface to read/write the T212 Portfolio Tracker sheet."""

    def __init__(self):
        creds = _get_credentials()
        self.sheets = build("sheets", "v4", credentials=creds)
        self.drive = build("drive", "v3", credentials=creds)
        self.sheet_id = _get_or_create_sheet(self.sheets, self.drive)
        self.url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit"

    # ── Portfolio tab ──────────────────────────────────────────────────────────

    def sync_portfolio(self, positions: list[dict[str, Any]]) -> None:
        """
        Sync T212 positions into the Portfolio tab.
        Merges with existing rows to preserve Priority and Created Date.
        """
        existing = self._read_tab(TAB_PORTFOLIO)
        # Build lookup by symbol
        existing_by_symbol: dict[str, dict] = {}
        for row in existing:
            if len(row) >= 3 and row[2]:  # Symbol in column C
                existing_by_symbol[row[2]] = row

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        rows = []
        for pos in positions:
            ticker = pos.get("ticker", "N/A")
            pie_name = pos.get("pieAccountName", "")
            market = _detect_market(ticker)
            qty = pos.get("quantity", 0)
            avg_price = pos.get("averagePrice", 0)
            current = pos.get("currentPrice", 0)
            ppl = pos.get("ppl", 0)

            old = existing_by_symbol.get(ticker, [])
            # Preserve existing analysis, priority, created date
            analysis = old[7] if len(old) > 7 else ""
            priority = old[8] if len(old) > 8 else "3"  # default priority
            last_updated = old[9] if len(old) > 9 else now
            created_date = old[10] if len(old) > 10 else now

            rows.append([
                pie_name, market, ticker, qty, avg_price, current, ppl,
                analysis, priority, last_updated, created_date,
            ])

        # Sort by priority (ascending = highest priority first)
        rows.sort(key=lambda r: int(r[8]) if str(r[8]).isdigit() else 3)

        # Write header + data
        all_rows = [PORTFOLIO_HEADERS] + rows
        self._write_tab(TAB_PORTFOLIO, all_rows)
        log.info("Portfolio tab synced with %d positions.", len(rows))

    def get_portfolio_for_analysis(self) -> list[dict]:
        """
        Returns portfolio stocks sorted by priority, with their current data.
        Only returns stocks that need analysis (priority > 0).
        """
        rows = self._read_tab(TAB_PORTFOLIO)
        stocks = []
        for row in rows:
            if len(row) < 3 or not row[2]:
                continue
            priority = int(row[8]) if len(row) > 8 and str(row[8]).isdigit() else 3
            if priority <= 0:
                continue
            stocks.append({
                "symbol": row[2],
                "market": row[1] if len(row) > 1 else "",
                "quantity": row[3] if len(row) > 3 else 0,
                "avg_price": row[4] if len(row) > 4 else 0,
                "current_price": row[5] if len(row) > 5 else 0,
                "ppl": row[6] if len(row) > 6 else 0,
                "priority": priority,
                "last_updated": row[9] if len(row) > 9 else "",
                "row_index": rows.index(row) + 1,  # 1-based for Sheets
            })
        # Sort: priority 1 first (most important), then by last updated (oldest first)
        stocks.sort(key=lambda s: (s["priority"], s.get("last_updated", "")))
        return stocks

    def update_portfolio_analysis(self, symbol: str, analysis: str) -> None:
        """Update the analysis result and timestamp for a portfolio stock."""
        rows = self._read_tab(TAB_PORTFOLIO)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for i, row in enumerate(rows):
            if len(row) >= 3 and row[2] == symbol:
                # Pad row if needed
                while len(row) < 11:
                    row.append("")
                row[7] = analysis       # Analysis Result
                row[9] = now            # Last Updated
                # Write just this row back (i+1 because header is row 1)
                self.sheets.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"'{TAB_PORTFOLIO}'!A{i + 1}",
                    valueInputOption="RAW",
                    body={"values": [row]},
                ).execute()
                return

    # ── Watchlist tab ──────────────────────────────────────────────────────────

    def get_watchlist(self) -> list[dict]:
        """Returns watchlist symbols that have been filled in by the user."""
        rows = self._read_tab(TAB_WATCHLIST)
        watchlist = []
        for i, row in enumerate(rows):
            if len(row) >= 1 and row[0]:
                watchlist.append({
                    "symbol": row[0],
                    "market": row[1] if len(row) > 1 else "",
                    "existing_analysis": row[2] if len(row) > 2 else "",
                    "row_index": i + 1,
                })
        return watchlist

    def update_watchlist_analysis(self, symbol: str, analysis: str) -> None:
        """Update analysis for a watchlist symbol."""
        rows = self._read_tab(TAB_WATCHLIST)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for i, row in enumerate(rows):
            if len(row) >= 1 and row[0] == symbol:
                while len(row) < 5:
                    row.append("")
                row[2] = analysis    # Analysis Result
                row[3] = now         # Last Updated
                if not row[4]:
                    row[4] = now     # Created Date (first time)
                self.sheets.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"'{TAB_WATCHLIST}'!A{i + 1}",
                    valueInputOption="RAW",
                    body={"values": [row]},
                ).execute()
                return

    # ── Market Overview tab ────────────────────────────────────────────────────

    def write_market_overview(self, entries: list[dict]) -> None:
        """
        Write daily market overview entries.
        Each entry: {date, region, index_sector, weight, analysis}
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        # Read existing to append (keep history)
        existing = self._read_tab(TAB_MARKET)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Remove today's existing entries (will be replaced)
        kept = [r for r in existing if not (len(r) >= 1 and r[0] == today)]

        new_rows = []
        for entry in entries:
            new_rows.append([
                today,
                entry.get("region", ""),
                entry.get("index_sector", ""),
                entry.get("weight", ""),
                entry.get("analysis", ""),
                now,
            ])

        all_rows = [MARKET_HEADERS] + kept + new_rows
        self._write_tab(TAB_MARKET, all_rows)
        log.info("Market Overview updated with %d entries.", len(new_rows))

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _read_tab(self, tab_name: str) -> list[list]:
        """Read all rows from a tab (excluding header)."""
        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"'{tab_name}'!A:Z",
            ).execute()
            rows = result.get("values", [])
            return rows[1:] if len(rows) > 1 else []  # skip header
        except HttpError as e:
            log.warning("Failed to read tab '%s': %s", tab_name, e)
            return []

    def _write_tab(self, tab_name: str, rows: list[list]) -> None:
        """Overwrite an entire tab with the given rows (including header)."""
        # Clear existing content
        self.sheets.spreadsheets().values().clear(
            spreadsheetId=self.sheet_id,
            range=f"'{tab_name}'!A:Z",
        ).execute()
        # Write new content
        if rows:
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=f"'{tab_name}'!A1",
                valueInputOption="RAW",
                body={"values": rows},
            ).execute()


def _detect_market(ticker: str) -> str:
    """Simple heuristic to detect US vs UK market from ticker format."""
    # T212 UK tickers often end with _EQ (e.g., BARC_EQ), or have suffixes like .L
    if ticker.endswith("_EQ") or ".L" in ticker or ticker.endswith("_LSE"):
        return "UK"
    # Most other tickers are US
    return "US"
