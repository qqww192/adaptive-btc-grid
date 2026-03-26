"""
sheets.py
Google Sheets integration for the T212 Portfolio Checker.

Three tabs:
  - Portfolio:       Pie | Symbol | Amount | Price | Weight % | Verdict | Fair Value | Risk | Key Note | Updated
  - Market Overview: Date | Category | Indicator | Value | Change % | Signal | Updated
  - Alerts:          Symbol | Metric | Condition | Threshold | Current | Status | Last Checked
                     (user fills first 4 columns, system fills last 3)

Authentication: Google Service Account via GOOGLE_SA_JSON env var.
Sheet ID: GOOGLE_SHEET_ID env var (must be set to an existing spreadsheet).
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

SHEET_ID: str = os.environ.get("GOOGLE_SHEET_ID", "")

# Tab names
TAB_PORTFOLIO = "Portfolio"
TAB_MARKET = "Market Overview"
TAB_ALERTS = "Alerts"

# Column headers
PORTFOLIO_HEADERS = [
    "Pie", "Symbol", "Amount", "Price", "Weight %",
    "Verdict", "Fair Value", "Risk", "Key Note", "Updated",
]

MARKET_HEADERS = [
    "Date", "Category", "Indicator", "Value", "Change %", "Signal", "Updated",
]

ALERT_HEADERS = [
    "Symbol", "Metric", "Condition", "Threshold",
    "Current", "Status", "Last Checked",
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


def _ensure_tabs_exist(sheets_service, sheet_id: str) -> None:
    """Ensure the three required tabs exist; create any that are missing."""
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing_tabs = {s["properties"]["title"] for s in spreadsheet["sheets"]}

    required_tabs = [TAB_PORTFOLIO, TAB_MARKET, TAB_ALERTS]
    missing = [t for t in required_tabs if t not in existing_tabs]
    if not missing:
        return

    requests = [{"addSheet": {"properties": {"title": tab}}} for tab in missing]
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()

    headers_map = {
        TAB_PORTFOLIO: PORTFOLIO_HEADERS,
        TAB_MARKET: MARKET_HEADERS,
        TAB_ALERTS: ALERT_HEADERS,
    }
    header_data = [
        {"range": f"'{tab}'!A1", "values": [headers_map[tab]]}
        for tab in missing
    ]
    if header_data:
        sheets_service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": header_data},
        ).execute()

    log.info("Created missing tabs: %s", ", ".join(missing))


class SheetManager:
    """High-level interface to read/write the T212 Portfolio Tracker sheet."""

    def __init__(self):
        if not SHEET_ID:
            raise EnvironmentError(
                "GOOGLE_SHEET_ID environment variable is not set. "
                "Set it to the ID of an existing Google Sheet shared with the service account."
            )
        creds = _get_credentials()
        self.sheets = build("sheets", "v4", credentials=creds)
        self.sheet_id = SHEET_ID
        _ensure_tabs_exist(self.sheets, self.sheet_id)
        self.url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit"

    # ── Portfolio tab ──────────────────────────────────────────────────────────
    # Columns: Pie | Symbol | Amount | Price | Weight % | Verdict | Fair Value | Risk | Key Note | Updated

    def sync_portfolio(self, positions: list[dict[str, Any]], prices: dict[str, float] | None = None) -> None:
        """
        Sync T212 positions into the Portfolio tab.
        Preserves existing analysis fields (Verdict, Fair Value, Risk, Key Note).
        """
        existing = self._read_tab(TAB_PORTFOLIO)
        existing_by_symbol: dict[str, list] = {}
        for row in existing:
            if len(row) >= 2 and row[1]:
                existing_by_symbol[row[1]] = row

        prices = prices or {}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        total_value = 0
        pos_data = []
        for pos in positions:
            ticker = pos.get("ticker", "N/A")
            qty = float(pos.get("quantity", 0))
            price = prices.get(ticker) or float(pos.get("currentPrice", 0))
            value = qty * price
            total_value += value
            pos_data.append((pos, ticker, qty, price, value))

        rows = []
        for pos, ticker, qty, price, value in pos_data:
            pie_name = pos.get("pieAccountName", "")
            weight = f"{value / total_value * 100:.1f}" if total_value else "0"

            old = existing_by_symbol.get(ticker, [])
            verdict = old[5] if len(old) > 5 else ""
            fair_val = old[6] if len(old) > 6 else ""
            risk = old[7] if len(old) > 7 else ""
            key_note = old[8] if len(old) > 8 else ""

            rows.append([pie_name, ticker, qty, price, weight, verdict, fair_val, risk, key_note, now])

        rows.sort(key=lambda r: float(r[4]) if r[4] else 0, reverse=True)

        all_rows = [PORTFOLIO_HEADERS] + rows
        self._write_tab(TAB_PORTFOLIO, all_rows)
        log.info("Portfolio tab synced with %d positions.", len(rows))

    def get_portfolio_for_analysis(self) -> list[dict]:
        """Returns portfolio stocks sorted by weight (highest first)."""
        rows = self._read_tab(TAB_PORTFOLIO)
        stocks = []
        for row in rows:
            if len(row) < 2 or not row[1]:
                continue
            stocks.append({
                "symbol": row[1],
                "pie": row[0] if row else "",
                "amount": row[2] if len(row) > 2 else 0,
                "price": row[3] if len(row) > 3 else 0,
                "weight": row[4] if len(row) > 4 else "0",
            })
        stocks.sort(key=lambda s: float(s.get("weight", 0)), reverse=True)
        return stocks

    def update_portfolio_analysis(self, symbol: str, verdict: str, fair_value: str,
                                   risk: str, key_note: str) -> None:
        """Update the analysis columns for a portfolio stock."""
        rows = self._read_tab(TAB_PORTFOLIO)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for i, row in enumerate(rows):
            if len(row) >= 2 and row[1] == symbol:
                while len(row) < 10:
                    row.append("")
                row[5] = verdict
                row[6] = fair_value
                row[7] = risk
                row[8] = key_note
                row[9] = now
                self.sheets.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"'{TAB_PORTFOLIO}'!A{i + 2}",
                    valueInputOption="RAW",
                    body={"values": [row]},
                ).execute()
                return

    # ── Market Overview tab ────────────────────────────────────────────────────
    # Columns: Date | Category | Indicator | Value | Change % | Signal | Updated

    def write_market_overview(self, entries: list[dict]) -> None:
        """
        Write market scorecard rows.
        Each entry: {category, name, value, change_pct, signal}
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        existing = self._read_tab(TAB_MARKET)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        kept = [r for r in existing if not (len(r) >= 1 and r[0] == today)]

        new_rows = []
        for e in entries:
            val = e.get("value", "")
            chg = e.get("change_pct", "")
            if isinstance(val, float):
                val = f"{val:,.2f}"
            if isinstance(chg, float):
                chg = f"{chg:+.2f}%"
            new_rows.append([
                today,
                e.get("category", ""),
                e.get("name", ""),
                val,
                chg,
                e.get("signal", ""),
                now,
            ])

        all_rows = [MARKET_HEADERS] + kept + new_rows
        self._write_tab(TAB_MARKET, all_rows)
        log.info("Market Overview updated with %d entries.", len(new_rows))

    def write_market_ai_summary(self, summary: str) -> None:
        """Append a row with the AI market interpretation."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = [today, "AI", "Market Summary", "", "", summary, now]
        self.sheets.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=f"'{TAB_MARKET}'!A:G",
            valueInputOption="RAW",
            body={"values": [row]},
        ).execute()

    # ── Alerts tab ─────────────────────────────────────────────────────────────
    # Columns: Symbol | Metric | Condition | Threshold | Current | Status | Last Checked
    # User fills columns A-D, system fills E-G

    def get_alerts(self) -> list[dict]:
        """Read alert rules set by the user (rows with at least Symbol + Metric + Condition + Threshold)."""
        rows = self._read_tab(TAB_ALERTS)
        alerts = []
        for i, row in enumerate(rows):
            if len(row) >= 4 and row[0] and row[1] and row[2] and row[3]:
                alerts.append({
                    "symbol": row[0],
                    "metric": row[1],
                    "condition": row[2],    # above / below
                    "threshold": row[3],
                    "row_index": i + 2,     # +2 for header + 0-indexed
                })
        return alerts

    def update_alert_status(self, row_index: int, current_value: float, triggered: bool) -> None:
        """Update columns E-G for an alert row."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        status = "TRIGGERED" if triggered else "OK"
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'{TAB_ALERTS}'!E{row_index}",
            valueInputOption="RAW",
            body={"values": [[f"{current_value:.2f}", status, now]]},
        ).execute()

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _read_tab(self, tab_name: str) -> list[list]:
        """Read all rows from a tab (excluding header)."""
        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.sheet_id,
                range=f"'{tab_name}'!A:Z",
            ).execute()
            rows = result.get("values", [])
            return rows[1:] if len(rows) > 1 else []
        except HttpError as e:
            log.warning("Failed to read tab '%s': %s", tab_name, e)
            return []

    def _write_tab(self, tab_name: str, rows: list[list]) -> None:
        """Overwrite an entire tab with the given rows (including header)."""
        self.sheets.spreadsheets().values().clear(
            spreadsheetId=self.sheet_id,
            range=f"'{tab_name}'!A:Z",
        ).execute()
        if rows:
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=f"'{tab_name}'!A1",
                valueInputOption="RAW",
                body={"values": rows},
            ).execute()
