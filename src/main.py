"""
T212 Portfolio Checker — Daily Orchestrator

Daily workflow (budget: 19 Gemini requests):
  1. Fetch portfolio from T212 and sync to Google Sheet
  2. Analyse watchlist symbols (if any filled by user)
  3. Basic market overview (1 request)
  4. Advanced individual stock analysis (remaining budget, by priority)
  5. Stop at 19 requests, update sheet, wait for next day
"""

import os
import sys
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

from fetch_portfolio import fetch_all_positions
from sheets import SheetManager
from analyse import (
    _get_client,
    AnalysisBudget,
    analyse_watchlist_symbol,
    analyse_market_overview,
    analyse_stock_advanced,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()

    log.info("=== T212 Portfolio Checker starting (daily run) ===")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Initialise services ────────────────────────────────────────────────
    gemini = _get_client()
    budget = AnalysisBudget()
    sheet = SheetManager()
    log.info("Sheet URL: %s", sheet.url)

    # ── Step 1: Fetch & sync portfolio ─────────────────────────────────────
    log.info("Step 1 — Fetching portfolio from Trading 212...")
    positions = fetch_all_positions()
    if not positions:
        log.warning("No positions returned. Continuing with existing sheet data.")
    else:
        log.info("  %d positions fetched. Syncing to sheet...", len(positions))
        sheet.sync_portfolio(positions)

    # ── Step 2: Analyse watchlist symbols ───────────────────────────────────
    log.info("Step 2 — Checking watchlist for symbols to analyse...")
    watchlist = sheet.get_watchlist()
    if watchlist:
        log.info("  %d watchlist symbols found.", len(watchlist))
        for item in watchlist:
            if budget.exhausted:
                log.info("  Budget exhausted. Remaining watchlist deferred to tomorrow.")
                break
            symbol = item["symbol"]
            log.info("  Analysing watchlist symbol: %s", symbol)
            if not budget.consume():
                break
            try:
                analysis = analyse_watchlist_symbol(gemini, symbol, item.get("market", ""))
                sheet.update_watchlist_analysis(symbol, analysis)
                log.info("  %s analysis written to sheet.", symbol)
            except Exception as e:
                log.error("  Failed to analyse %s: %s", symbol, e)
    else:
        log.info("  No watchlist symbols to analyse.")

    # ── Step 3: Basic market overview ──────────────────────────────────────
    if not budget.exhausted:
        log.info("Step 3 — Running basic market overview...")
        if not budget.consume():
            log.info("  Budget exhausted. Market overview deferred.")
        else:
            try:
                market_positions = positions or []
                entries = analyse_market_overview(gemini, market_positions)
                sheet.write_market_overview(entries)
                log.info("  Market overview written to sheet.")
            except Exception as e:
                log.error("  Market overview failed: %s", e)
    else:
        log.info("Step 3 — Skipped (budget exhausted).")

    # ── Step 4: Advanced individual stock analysis ─────────────────────────
    if not budget.exhausted:
        log.info("Step 4 — Running advanced stock analysis (budget: %d remaining)...", budget.remaining)
        stocks = sheet.get_portfolio_for_analysis()
        analysed = 0
        for stock in stocks:
            if budget.exhausted:
                log.info("  Budget exhausted after %d stocks. Rest deferred to tomorrow.", analysed)
                break
            symbol = stock["symbol"]
            log.info("  Analysing %s (priority %d)...", symbol, stock["priority"])
            if not budget.consume():
                break
            try:
                analysis = analyse_stock_advanced(
                    gemini,
                    symbol=symbol,
                    market=stock.get("market", ""),
                    quantity=stock.get("quantity", 0),
                    avg_price=stock.get("avg_price", 0),
                    current_price=stock.get("current_price", 0),
                    ppl=stock.get("ppl", 0),
                )
                sheet.update_portfolio_analysis(symbol, analysis)
                log.info("  %s analysis written to sheet.", symbol)
                analysed += 1
            except Exception as e:
                log.error("  Failed to analyse %s: %s", symbol, e)
    else:
        log.info("Step 4 — Skipped (budget exhausted).")

    # ── Summary ────────────────────────────────────────────────────────────
    log.info(
        "=== Pipeline complete — %d/%d Gemini requests used ===",
        budget.used, budget.max_requests,
    )
    log.info("Sheet: %s", sheet.url)


if __name__ == "__main__":
    main()
