"""
sheets.py
Google Sheets integration for the T212 Portfolio Checker.

Three tabs:
  - Portfolio:  Symbol | Qty | Avg Price | Current Price | P/L | Value | Weight %
                | Verdict | Fair Value | Risk | Key Note | Updated
  - Signals:    Date | Type | Indicator | Value | Reading | Signal
                | Success Rate | Timeframe | Updated
  - Alerts:     Symbol | Metric | Condition | Threshold | Type
                | Current | Status | Last Checked
                (user fills A-E, system fills F-H)

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

# Tab names
TAB_PORTFOLIO = "Portfolio"
TAB_SIGNALS = "Signals"
TAB_ALERTS = "Alerts"

# Column headers
PORTFOLIO_HEADERS = [
    "Pie", "Symbol", "Name", "Qty", "Avg Price", "Current Price", "P/L",
    "Value", "Weight %", "AI Analyse",
    "Verdict", "Fair Value", "Risk", "Key Note", "Price Updated", "AI Updated",
]

SIGNAL_HEADERS = [
    "Date", "Type", "Indicator", "Value", "Reading", "Signal",
    "Success Rate", "Timeframe", "Updated",
]

ALERT_HEADERS = [
    "Symbol", "Metric", "Condition", "Threshold", "Type",
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
    """Ensure the required tabs exist; create any that are missing."""
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing_tabs = {s["properties"]["title"] for s in spreadsheet["sheets"]}

    required_tabs = [TAB_PORTFOLIO, TAB_SIGNALS, TAB_ALERTS]
    missing = [t for t in required_tabs if t not in existing_tabs]
    if not missing:
        return

    requests = [{"addSheet": {"properties": {"title": tab}}} for tab in missing]
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()

    headers_map = {
        TAB_PORTFOLIO: PORTFOLIO_HEADERS,
        TAB_SIGNALS: SIGNAL_HEADERS,
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
        sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
        if not sheet_id:
            raise EnvironmentError(
                "GOOGLE_SHEET_ID environment variable is not set. "
                "Set it to the ID of an existing Google Sheet shared with the service account."
            )
        creds = _get_credentials()
        self.sheets = build("sheets", "v4", credentials=creds)
        self.sheet_id = sheet_id
        _ensure_tabs_exist(self.sheets, self.sheet_id)
        self.url = f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit"

    # ── Portfolio tab ──────────────────────────────────────────────────────────
    # Columns: Pie | Symbol | Name | Qty | Avg Price | Current Price | P/L
    #          | Value | Weight % | AI Analyse | Verdict | Fair Value | Risk
    #          | Key Note | Price Updated | AI Updated

    def sync_portfolio(self, positions: list[dict[str, Any]], prices: dict[str, float] | None = None) -> None:
        """
        Sync T212 positions into the Portfolio tab.
        Positions come from fetch_portfolio.fetch_portfolio() (pies endpoint).
        Preserves: AI Analyse checkbox, analysis fields, and AI Updated timestamp.
        Only updates Price Updated timestamp.
        """
        existing = self._read_tab(TAB_PORTFOLIO)
        # Key by symbol (col B, index 1)
        existing_by_symbol: dict[str, list] = {}
        for row in existing:
            if len(row) >= 2 and row[1]:
                existing_by_symbol[row[1]] = row

        prices = prices or {}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        total_value = 0
        pos_data = []
        for pos in positions:
            ticker = pos.get("ticker", "")
            if not ticker:
                continue
            pie_name = pos.get("pieName", "")
            name = pos.get("name", "")
            qty = float(pos.get("quantity", 0))
            avg_price = float(pos.get("averagePrice", 0))
            current_price = prices.get(ticker) or float(pos.get("currentPrice", 0))
            ppl = float(pos.get("ppl", 0))
            value = float(pos.get("value", 0)) or (qty * current_price)
            total_value += value
            pos_data.append((pie_name, ticker, name, qty, avg_price, current_price, ppl, value))

        rows = []
        for pie_name, ticker, name, qty, avg_price, current_price, ppl, value in pos_data:
            weight = f"{value / total_value * 100:.1f}" if total_value else "0"

            old = existing_by_symbol.get(ticker, [])
            # Preserve user-set AI Analyse checkbox (index 9)
            ai_analyse = old[9] if len(old) > 9 else ""
            # Analysis columns at indices 10-13
            verdict = old[10] if len(old) > 10 else ""
            fair_val = old[11] if len(old) > 11 else ""
            risk = old[12] if len(old) > 12 else ""
            key_note = old[13] if len(old) > 13 else ""
            # Preserve AI Updated timestamp (index 15)
            ai_updated = old[15] if len(old) > 15 else ""

            rows.append([
                pie_name, ticker, name, qty,
                f"{avg_price:.2f}", f"{current_price:.2f}",
                f"{ppl:.2f}", f"{value:.2f}", weight,
                ai_analyse,
                verdict, fair_val, risk, key_note,
                now,        # Price Updated
                ai_updated, # AI Updated (preserved)
            ])

        rows.sort(key=lambda r: float(r[8]) if r[8] else 0, reverse=True)

        all_rows = [PORTFOLIO_HEADERS] + rows
        self._write_tab(TAB_PORTFOLIO, all_rows)
        self._set_portfolio_checkboxes(len(rows))
        log.info("Portfolio tab synced with %d positions. Total: £%.2f", len(rows), total_value)

    def _set_portfolio_checkboxes(self, num_rows: int) -> None:
        """Apply checkbox data validation to AI Analyse column (J) for all data rows."""
        if num_rows <= 0:
            return
        try:
            # Get the Portfolio tab's sheet ID (not the spreadsheet ID)
            spreadsheet = self.sheets.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
            tab_sheet_id = None
            for s in spreadsheet["sheets"]:
                if s["properties"]["title"] == TAB_PORTFOLIO:
                    tab_sheet_id = s["properties"]["sheetId"]
                    break
            if tab_sheet_id is None:
                return

            request = {
                "setDataValidation": {
                    "range": {
                        "sheetId": tab_sheet_id,
                        "startRowIndex": 1,  # skip header
                        "endRowIndex": 1 + num_rows,
                        "startColumnIndex": 9,  # column J
                        "endColumnIndex": 10,
                    },
                    "rule": {
                        "condition": {"type": "BOOLEAN"},
                        "showCustomUi": True,
                    },
                }
            }
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.sheet_id,
                body={"requests": [request]},
            ).execute()
        except Exception as e:
            log.warning("Failed to set checkboxes: %s", e)

    def get_portfolio_for_analysis(self, max_tickers: int = 15) -> list[dict]:
        """
        Returns portfolio stocks that have AI Analyse checked,
        sorted by stalest AI Updated timestamp first (oldest/empty first).
        Limited to max_tickers per run.
        """
        rows = self._read_tab(TAB_PORTFOLIO)
        stocks = []
        for row in rows:
            if len(row) < 2 or not row[1]:
                continue
            ai_analyse = (row[9].strip().upper() if len(row) > 9 and row[9] else "")
            # Only include rows where AI Analyse is checked (TRUE, YES, Y, 1, X, ✓)
            if ai_analyse not in ("TRUE", "YES", "Y", "1", "X", "✓"):
                continue
            stocks.append({
                "pie": row[0] if row else "",
                "symbol": row[1],
                "name": row[2] if len(row) > 2 else "",
                "qty": row[3] if len(row) > 3 else 0,
                "avg_price": row[4] if len(row) > 4 else 0,
                "price": row[5] if len(row) > 5 else 0,
                "ppl": row[6] if len(row) > 6 else 0,
                "value": row[7] if len(row) > 7 else 0,
                "weight": row[8] if len(row) > 8 else "0",
                "ai_updated": row[15] if len(row) > 15 and row[15] else "",
            })
        # Sort by stalest AI Updated (empty string sorts first = never analysed)
        stocks.sort(key=lambda s: s.get("ai_updated", ""))
        return stocks[:max_tickers]

    def update_portfolio_analysis(self, symbol: str, verdict: str, fair_value: str,
                                   risk: str, key_note: str) -> None:
        """Update the analysis columns for a portfolio stock (cols K-P)."""
        rows = self._read_tab(TAB_PORTFOLIO)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        for i, row in enumerate(rows):
            # Symbol is column B (index 1)
            if len(row) >= 2 and row[1] == symbol:
                while len(row) < 16:
                    row.append("")
                # Index 9 = AI Analyse (preserved, don't touch)
                row[10] = verdict
                row[11] = fair_value
                row[12] = risk
                row[13] = key_note
                # Index 14 = Price Updated (preserved, don't touch)
                row[15] = now  # AI Updated
                self.sheets.spreadsheets().values().update(
                    spreadsheetId=self.sheet_id,
                    range=f"'{TAB_PORTFOLIO}'!A{i + 2}",
                    valueInputOption="RAW",
                    body={"values": [row]},
                ).execute()
                return

    # ── Signals tab ─────────────────────────────────────────────────────────────
    # Columns: Date | Type | Indicator | Value | Reading | Signal
    #          | Success Rate | Timeframe | Updated
    # Type: "Market" for scorecard entries, "Signal" for computed signals

    def write_signals(self, signals: list[dict], signal_type: str = "Signal") -> None:
        """
        Write signal/market metrics to the Signals tab.
        Each entry: {name, value, reading, signal, success_rate, timeframe}
        signal_type: "Signal" for computed signals, "Market" for market scorecard
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        existing = self._read_tab(TAB_SIGNALS)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Keep rows from other dates, and same-date rows of a different type
        kept = [r for r in existing
                if not (len(r) >= 2 and r[0] == today and r[1] == signal_type)]

        new_rows = []
        for s in signals:
            val = s.get("value", "")
            if isinstance(val, float):
                val = f"{val:,.2f}"
            chg = s.get("change_pct", "")
            if isinstance(chg, float):
                chg = f"{chg:+.2f}%"

            reading = s.get("reading", "")
            if not reading and chg:
                reading = str(chg)

            new_rows.append([
                today,
                signal_type,
                s.get("name", ""),
                val,
                reading,
                s.get("signal", ""),
                s.get("success_rate", ""),
                s.get("timeframe", ""),
                now,
            ])

        all_rows = [SIGNAL_HEADERS] + kept + new_rows
        self._write_tab(TAB_SIGNALS, all_rows)
        log.info("Signals tab updated: %d %s entries.", len(new_rows), signal_type)

    def write_signal_ai_summary(self, summary: str) -> None:
        """Write AI Summary row to Signals tab, replacing today's existing entry."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = self._read_tab(TAB_SIGNALS)
        # Remove any existing AI Summary for today
        kept = [r for r in existing
                if not (len(r) >= 2 and r[0] == today and r[1] == "AI Summary")]
        new_row = [today, "AI Summary", "Market Summary", "", "", summary, "", "", now]
        all_rows = [SIGNAL_HEADERS] + kept + [new_row]
        self._write_tab(TAB_SIGNALS, all_rows)
        log.info("AI Summary written to Signals tab.")

    # ── Alerts tab ─────────────────────────────────────────────────────────────
    # Columns: Symbol | Metric | Condition | Threshold | Type
    #          | Current | Status | Last Checked
    # User fills columns A-E, system fills F-H
    # Type: "one-time" or "recurring" (default: recurring)

    def get_alerts(self) -> list[dict]:
        """Read alert rules (rows with at least Symbol + Metric + Condition + Threshold)."""
        rows = self._read_tab(TAB_ALERTS)
        alerts = []
        for i, row in enumerate(rows):
            if len(row) >= 4 and row[0] and row[1] and row[2] and row[3]:
                alert_type = row[4].strip().lower() if len(row) > 4 and row[4] else "recurring"
                # Skip one-time alerts that were already triggered
                status = row[6].strip().upper() if len(row) > 6 and row[6] else ""
                if alert_type == "one-time" and status == "TRIGGERED":
                    continue
                alerts.append({
                    "symbol": row[0],
                    "metric": row[1],
                    "condition": row[2],
                    "threshold": row[3],
                    "type": alert_type,
                    "row_index": i + 2,     # +2 for header + 0-indexed
                })
        return alerts

    def update_alert_status(self, row_index: int, current_value: float, triggered: bool) -> None:
        """Update columns F-H for an alert row."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        status = "TRIGGERED" if triggered else "OK"
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'{TAB_ALERTS}'!F{row_index}",
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
