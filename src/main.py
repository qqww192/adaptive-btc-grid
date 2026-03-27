"""
T212 Portfolio Checker — Daily Orchestrator

Pipeline priority:
  1. Evaluate alerts first → Telegram if triggered
  2. Compute signal metrics → Signals tab + AI summary
  3. Update portfolio from T212 (via pies endpoint)
  4. AI analysis on portfolio; if risk is high → Telegram
  5. If alerts, signals, or AI show high risk → Telegram summary
"""

import os
import sys
import logging
from dotenv import load_dotenv

from fetch_portfolio import fetch_all_positions, t212_to_yfinance
from sheets import SheetManager
from market_data import (
    get_batch_prices,
    get_signal_metrics,
    build_stock_context,
    evaluate_alert,
)
from analyse import (
    _get_client,
    AnalysisBudget,
    analyse_market_overview,
    analyse_stock,
)
import telegram_notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

HIGH_RISK_THRESHOLD = 7  # risk score >= this triggers Telegram


def main() -> None:
    load_dotenv()

    log.info("=== T212 Portfolio Checker starting ===")

    gemini = _get_client()
    budget = AnalysisBudget()
    sheet = SheetManager()
    log.info("Sheet URL: %s", sheet.url)

    high_risk_items: list[str] = []

    # ── Step 1: Evaluate alerts ────────────────────────────────────────────
    log.info("Step 1 — Evaluating alerts...")
    alerts = sheet.get_alerts()
    if alerts:
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
                    log.warning("  Unknown condition '%s' for %s", condition, alert["symbol"])
                    continue

                sheet.update_alert_status(alert["row_index"], current, triggered)
                status = "TRIGGERED" if triggered else "OK"
                log.info("  %s %s %s %s → %s (current: %.2f)",
                         alert["symbol"], alert["metric"], condition, alert["threshold"],
                         status, current)

                if triggered:
                    telegram_notify.notify_alert_triggered(alert, current)
                    high_risk_items.append(
                        f"Alert: {alert['symbol']} {alert['metric']} {condition} "
                        f"{alert['threshold']} (current: {current:.2f})"
                    )
            except Exception as e:
                log.error("  Alert check failed for %s: %s", alert["symbol"], e)
    else:
        log.info("  No alerts configured.")

    # ── Step 2: Signal metrics → Signals tab ──────────────────────────────
    log.info("Step 2 — Computing signal metrics...")
    signals_summary = ""
    try:
        signals = get_signal_metrics()
        sheet.write_signals(signals, signal_type="Signal")
        log.info("  %d signals computed.", len(signals))
        for s in signals:
            log.info("    %-25s %10s  [%s]", s["name"], s["value"], s["signal"])

        # Build summary for AI
        signal_lines = []
        for s in signals:
            signal_lines.append(f"{s['name']}: {s['value']} [{s['signal']}] ({s.get('reading', '')})")
        signals_summary = "\n".join(signal_lines)

        # Check Fear & Greed for extreme fear
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
    except Exception as e:
        log.error("  Signal metrics failed: %s", e)

    # 2b: AI interpretation of signals
    if not budget.exhausted and signals_summary:
        log.info("Step 2b — AI signal interpretation...")
        if budget.consume():
            try:
                positions_for_ai = []  # will be filled in step 3
                ai_summary = analyse_market_overview(gemini, signals_summary, positions_for_ai)
                sheet.write_signal_ai_summary(ai_summary)
                log.info("  AI signals summary: %s", ai_summary[:120])
            except Exception as e:
                log.error("  AI signal interpretation failed: %s", e)

    # ── Step 3: Fetch & sync portfolio from T212 pies ─────────────────────
    log.info("Step 3 — Fetching portfolio from Trading 212 (pies)...")
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions returned. Continuing with existing sheet data.")
    else:
        log.info("  %d positions fetched. Getting live prices...", len(positions))
        # Map T212 tickers to yfinance format for price lookup
        t212_tickers = [p["ticker"] for p in positions if p.get("ticker")]
        yf_tickers = [t212_to_yfinance(t) for t in t212_tickers]
        yf_prices = get_batch_prices(yf_tickers)
        # Map prices back to T212 ticker keys
        live_prices = {}
        for t212_t, yf_t in zip(t212_tickers, yf_tickers):
            if yf_t in yf_prices and yf_prices[yf_t]:
                live_prices[t212_t] = yf_prices[yf_t]
        log.info("  Live prices fetched for %d/%d symbols.", len(live_prices), len(t212_tickers))
        sheet.sync_portfolio(positions, prices=live_prices)

    # ── Step 4: AI stock analysis ─────────────────────────────────────────
    if not budget.exhausted:
        log.info("Step 4 — Analysing stocks (budget: %d remaining)...", budget.remaining)
        stocks = sheet.get_portfolio_for_analysis()
        analysed = 0
        for stock in stocks:
            if budget.exhausted:
                log.info("  Budget exhausted after %d stocks.", analysed)
                break
            symbol = stock["symbol"]
            yf_symbol = t212_to_yfinance(symbol)
            log.info("  Analysing %s → %s (weight %s%%)...", symbol, yf_symbol, stock.get("weight", "0"))
            if not budget.consume():
                break
            try:
                context = build_stock_context(yf_symbol)
                result = analyse_stock(
                    gemini,
                    symbol=symbol,
                    financial_context=context,
                    amount=stock.get("qty", 0),
                    price=stock.get("price", 0),
                    weight=stock.get("weight", "0"),
                    market_context=signals_summary,
                )
                sheet.update_portfolio_analysis(
                    symbol,
                    verdict=result["verdict"],
                    fair_value=result["fair_value"],
                    risk=result["risk"],
                    key_note=result["key_note"],
                )
                log.info("  %s → %s (fair: $%s, risk: %s/10)",
                         symbol, result["verdict"], result["fair_value"], result["risk"])
                analysed += 1

                # Check for high-risk stocks
                try:
                    risk_score = int(result["risk"])
                    if risk_score >= HIGH_RISK_THRESHOLD:
                        telegram_notify.notify_portfolio_risk(
                            symbol, result["risk"], result["verdict"], result["key_note"],
                        )
                        high_risk_items.append(
                            f"Stock: {symbol} risk={result['risk']}/10 ({result['verdict']})"
                        )
                except (ValueError, TypeError):
                    pass
            except Exception as e:
                log.error("  Failed to analyse %s: %s", symbol, e)
    else:
        log.info("Step 4 — Skipped (budget exhausted).")

    # ── Step 5: Final Telegram summary if any high-risk items ──────────────
    if high_risk_items:
        log.info("Step 5 — Sending risk summary to Telegram (%d items)...", len(high_risk_items))
        summary_lines = "\n".join(f"- {item}" for item in high_risk_items)
        telegram_notify.notify_high_risk(
            "Daily Risk Summary",
            f"{len(high_risk_items)} risk item(s) detected:\n\n{summary_lines}",
        )
    else:
        log.info("Step 5 — No high-risk items. No Telegram summary needed.")

    # ── Summary ────────────────────────────────────────────────────────────
    log.info(
        "=== Done — %d/%d Gemini requests used ===",
        budget.used, budget.max_requests,
    )
    log.info("Sheet: %s", sheet.url)


if __name__ == "__main__":
    main()
