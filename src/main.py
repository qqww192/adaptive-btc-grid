"""
T212 Portfolio Checker — Daily Orchestrator

Pipeline priority:
  1. Evaluate alerts first → Telegram if triggered
  2. Get market data and update Signals tab
  3. Update portfolio from T212
  4. AI analysis on portfolio; if risk is high → Telegram
  5. If alerts, signals, or AI show high risk → Telegram summary
"""

import os
import sys
import logging
from dotenv import load_dotenv

from fetch_portfolio import fetch_all_positions
from sheets import SheetManager
from market_data import (
    get_batch_prices,
    get_market_scorecard,
    get_signal_metrics,
    build_stock_context,
    build_market_summary,
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

    # ── Step 2: Market scorecard + Signal metrics → Signals tab ────────────
    log.info("Step 2 — Building market scorecard...")
    market_summary = ""
    try:
        scorecard = get_market_scorecard()
        sheet.write_market_overview(scorecard)
        market_summary = build_market_summary(scorecard)
        log.info("  Market scorecard written (%d indicators).", len(scorecard))

        # Check for danger signals in scorecard
        for entry in scorecard:
            if entry.get("name") == "Market Health" and entry.get("signal") in ("Danger", "Stressed"):
                msg = f"Market Health: {entry['value']}/100 ({entry['signal']})"
                high_risk_items.append(msg)
                telegram_notify.notify_high_risk("Market Health", msg)
    except Exception as e:
        log.error("  Market scorecard failed: %s", e)
        scorecard = []

    log.info("Step 2b — Computing signal metrics...")
    try:
        signals = get_signal_metrics()
        sheet.write_signals(signals, signal_type="Signal")
        log.info("  %d signals computed.", len(signals))
        for s in signals:
            log.info("    %-25s %10s  [%s]", s["name"], s["value"], s["signal"])

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

    # ── Step 3: Fetch & sync portfolio ─────────────────────────────────────
    log.info("Step 3 — Fetching portfolio from Trading 212...")
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions returned. Continuing with existing sheet data.")
    else:
        log.info("  %d positions fetched. Getting live prices...", len(positions))
        symbols = [p.get("ticker", "") for p in positions if p.get("ticker")]
        live_prices = get_batch_prices(symbols)
        log.info("  Live prices fetched for %d symbols.", len(live_prices))
        sheet.sync_portfolio(positions, prices=live_prices)

    # ── Step 4: AI analysis (structured) ───────────────────────────────────
    # 4a: AI market interpretation (1 Gemini request)
    if not budget.exhausted and market_summary:
        log.info("Step 4a — AI market interpretation...")
        if budget.consume():
            try:
                market_positions = positions or []
                ai_summary = analyse_market_overview(gemini, market_summary, market_positions)
                log.info("  AI summary: %s", ai_summary[:100])
            except Exception as e:
                log.error("  Market AI interpretation failed: %s", e)

    # 4b: AI stock analysis
    if not budget.exhausted:
        log.info("Step 4b — Analysing stocks (budget: %d remaining)...", budget.remaining)
        stocks = sheet.get_portfolio_for_analysis()
        analysed = 0
        for stock in stocks:
            if budget.exhausted:
                log.info("  Budget exhausted after %d stocks.", analysed)
                break
            symbol = stock["symbol"]
            log.info("  Analysing %s (weight %s%%)...", symbol, stock.get("weight", "0"))
            if not budget.consume():
                break
            try:
                context = build_stock_context(symbol)
                result = analyse_stock(
                    gemini,
                    symbol=symbol,
                    financial_context=context,
                    amount=stock.get("qty", 0),
                    price=stock.get("price", 0),
                    weight=stock.get("weight", "0"),
                    market_context=market_summary,
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
        log.info("Step 4b — Skipped (budget exhausted).")

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
