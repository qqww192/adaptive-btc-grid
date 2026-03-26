"""
test_columns.py
Test script to set up sheet structure and verify market data fetching.
Run this first before the full pipeline.

Usage: cd src && python test_columns.py
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
    log.info("Sheet Structure & Market Data Test")
    log.info("=" * 60)

    # ── Step 1: Connect & verify tabs ────────────────────────────────────
    log.info("\nSTEP 1: Connecting to Google Sheet...")
    try:
        from sheets import SheetManager, TAB_PORTFOLIO, TAB_SIGNALS, TAB_ALERTS
        from sheets import PORTFOLIO_HEADERS, SIGNAL_HEADERS, ALERT_HEADERS
        sheet = SheetManager()
        log.info("  Connected: %s", sheet.url)
    except Exception as e:
        log.error("  FAILED: %s", e)
        sys.exit(1)

    # ── Step 2: Update headers ───────────────────────────────────────────
    log.info("\nSTEP 2: Setting headers...")
    for tab, headers in [
        (TAB_PORTFOLIO, PORTFOLIO_HEADERS),
        (TAB_SIGNALS, SIGNAL_HEADERS),
        (TAB_ALERTS, ALERT_HEADERS),
    ]:
        try:
            result = sheet.sheets.spreadsheets().values().get(
                spreadsheetId=sheet.sheet_id,
                range=f"'{tab}'!A1:Z1",
            ).execute()
            actual = result.get("values", [[]])[0]
            if actual == headers:
                log.info("  %s: OK — %s", tab, headers)
            else:
                log.info("  %s: Updating headers...", tab)
                sheet.sheets.spreadsheets().values().clear(
                    spreadsheetId=sheet.sheet_id,
                    range=f"'{tab}'!A:Z",
                ).execute()
                sheet.sheets.spreadsheets().values().update(
                    spreadsheetId=sheet.sheet_id,
                    range=f"'{tab}'!A1",
                    valueInputOption="RAW",
                    body={"values": [headers]},
                ).execute()
                log.info("  %s: Headers set — %s", tab, headers)
        except Exception as e:
            log.error("  %s: error — %s", tab, e)

    # ── Step 3: Test market scorecard ────────────────────────────────────
    log.info("\nSTEP 3: Fetching market scorecard...")
    try:
        from market_data import get_market_scorecard, build_market_summary

        scorecard = get_market_scorecard()
        log.info("  %d indicators fetched:", len(scorecard))
        for row in scorecard:
            val = row["value"]
            chg = row.get("change_pct", "")
            if isinstance(val, (int, float)) and isinstance(chg, (int, float)):
                log.info("    %-15s %10.2f  %+.2f%%  [%s]", row["name"], val, chg, row["signal"])
            else:
                log.info("    %-15s %10s         [%s]", row["name"], val, row["signal"])

        summary = build_market_summary(scorecard)
        log.info("\n  Market summary for AI:\n  %s", summary.replace("\n", "\n  "))
    except Exception as e:
        log.error("  Market scorecard failed: %s", e)
        sys.exit(1)

    # ── Step 4: Write scorecard to Signals tab ───────────────────────────
    log.info("\nSTEP 4: Writing scorecard to Signals tab...")
    try:
        sheet.write_market_overview(scorecard)
        log.info("  Done.")
    except Exception as e:
        log.error("  Failed: %s", e)

    # ── Step 5: Signal metrics ───────────────────────────────────────────
    log.info("\nSTEP 5: Computing high-reliability signal metrics...")
    try:
        from market_data import get_signal_metrics

        signals = get_signal_metrics()
        log.info("  %d signals computed:", len(signals))
        for s in signals:
            log.info("    %-25s %10s  %-20s [%s] (%s)",
                     s["name"], s["value"], s["reading"], s["signal"], s["success_rate"])

        sheet.write_signals(signals, signal_type="Signal")
        log.info("  Signals written to sheet.")
    except Exception as e:
        log.error("  Signal metrics failed: %s", e)

    # ── Step 6: Test alert evaluation ────────────────────────────────────
    log.info("\nSTEP 6: Testing alert evaluation...")
    try:
        from market_data import evaluate_alert

        test_cases = [
            ("VIX", "price", "VIX level"),
            ("AAPL", "price", "AAPL price"),
            ("^TNX", "price", "10Y yield"),
        ]
        for sym, metric, label in test_cases:
            val = evaluate_alert(sym, metric)
            log.info("  %s: %.2f", label, val)

        alerts = sheet.get_alerts()
        if alerts:
            log.info("  %d alert rules found in sheet.", len(alerts))
        else:
            log.info("  No alert rules yet. Add rules in the Alerts tab:")
            log.info("    Column A: Symbol (e.g. AAPL, VIX, GOLD)")
            log.info("    Column B: Metric (price, pe, change%%)")
            log.info("    Column C: Condition (above or below)")
            log.info("    Column D: Threshold (e.g. 30, 150.00)")
            log.info("    Column E: Type (one-time or recurring)")
    except Exception as e:
        log.error("  Alert test failed: %s", e)

    # ── Summary ──────────────────────────────────────────────────────────
    log.info("\n" + "=" * 60)
    log.info("ALL DONE")
    log.info("=" * 60)
    log.info("Sheet: %s", sheet.url)
    log.info("")
    log.info("Tab structure:")
    log.info("  Portfolio: %s", PORTFOLIO_HEADERS)
    log.info("  Signals:   %s", SIGNAL_HEADERS)
    log.info("  Alerts:    %s", ALERT_HEADERS)
    log.info("")
    log.info("Alert examples (fill in Alerts tab):")
    log.info("  VIX     | price   | above | 25  | recurring")
    log.info("  AAPL    | price   | below | 150 | one-time")
    log.info("  US10Y   | price   | above | 5   | recurring")
    log.info("  TSLA    | pe      | above | 80  | one-time")
    log.info("  SP500   | change%% | below | -2  | recurring")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
