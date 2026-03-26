"""
T212 Portfolio Checker — Daily Orchestrator

Daily workflow:
  1. Fetch portfolio from T212, get live prices via yfinance, sync to sheet
  2. Fetch full market scorecard (VIX, yields, DXY, gold, oil, indices) + health score
  3. AI market interpretation (1 Gemini request)
  4. AI stock analysis — structured: verdict, fair value, risk, key note (by weight)
  5. Evaluate user-defined alerts (no Gemini needed)
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    log.info("=== T212 Portfolio Checker starting ===")

    gemini = _get_client()
    budget = AnalysisBudget()
    sheet = SheetManager()
    log.info("Sheet URL: %s", sheet.url)

    # ── Step 1: Fetch & sync portfolio ───────────────────────────────────
    log.info("Step 1 — Fetching portfolio from Trading 212...")
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions returned. Continuing with existing sheet data.")
    else:
        log.info("  %d positions fetched. Getting live prices...", len(positions))
        symbols = [p.get("ticker", "") for p in positions if p.get("ticker")]
        live_prices = get_batch_prices(symbols)
        log.info("  Live prices fetched for %d symbols.", len(live_prices))
        sheet.sync_portfolio(positions, prices=live_prices)

    # ── Step 2: Market scorecard (all yfinance, no Gemini) ───────────────
    log.info("Step 2 — Building market scorecard...")
    try:
        scorecard = get_market_scorecard()
        sheet.write_market_overview(scorecard)
        market_summary = build_market_summary(scorecard)
        log.info("  Market scorecard written (%d indicators).", len(scorecard))
    except Exception as e:
        log.error("  Market scorecard failed: %s", e)
        scorecard = []
        market_summary = ""

    # ── Step 3: AI market interpretation (1 Gemini request) ──────────────
    if not budget.exhausted and market_summary:
        log.info("Step 3 — AI market interpretation...")
        if budget.consume():
            try:
                market_positions = positions or []
                ai_summary = analyse_market_overview(gemini, market_summary, market_positions)
                # Append AI summary as the last row in market overview
                sheet.write_market_ai_summary(ai_summary)
                log.info("  AI summary: %s", ai_summary[:100])
            except Exception as e:
                log.error("  Market AI interpretation failed: %s", e)
    else:
        log.info("Step 3 — Skipped.")

    # ── Step 4: Stock analysis (structured) ──────────────────────────────
    if not budget.exhausted:
        log.info("Step 4 — Analysing stocks (budget: %d remaining)...", budget.remaining)
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
                    amount=stock.get("amount", 0),
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
            except Exception as e:
                log.error("  Failed to analyse %s: %s", symbol, e)
    else:
        log.info("Step 4 — Skipped (budget exhausted).")

    # ── Step 5: Evaluate alerts (yfinance only, no Gemini) ───────────────
    log.info("Step 5 — Evaluating alerts...")
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
            except Exception as e:
                log.error("  Alert check failed for %s: %s", alert["symbol"], e)
    else:
        log.info("  No alerts configured. Add rules in the Alerts tab.")

    # ── Summary ──────────────────────────────────────────────────────────
    log.info(
        "=== Done — %d/%d Gemini requests used ===",
        budget.used, budget.max_requests,
    )
    log.info("Sheet: %s", sheet.url)


if __name__ == "__main__":
    main()
