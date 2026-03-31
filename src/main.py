"""
T212 Portfolio Checker — Unified Pipeline

Runs both daily (cron) and manual triggers from a single script.
Use --no-ai to skip Gemini analysis (saves API quota).

Pipeline:
  1. Evaluate alerts → Telegram if triggered
  2. Compute signal metrics → Signals tab (+ AI summary if enabled)
  3. Fetch portfolio from T212 (all pies) → Portfolio tab
  3.5. Stock scanner → Scanner Results tab (if conditions configured)
  4. AI stock analysis (if enabled) → checkbox + stalest first, max 15
  5. Final Telegram summary if any high-risk items

Usage:
  python main.py            # full pipeline with AI
  python main.py --no-ai    # data only, no Gemini calls
"""

import argparse
import os
import sys
import logging
from dotenv import load_dotenv

from fetch_portfolio import fetch_portfolio, t212_to_yfinance
from sheets import SheetManager
from market_data import (
    get_batch_info,
    get_market_scorecard,
    get_signal_metrics,
    build_stock_context,
    evaluate_alert,
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
    parser = argparse.ArgumentParser(description="T212 Portfolio Checker")
    parser.add_argument(
        "--no-ai", action="store_true",
        help="Skip Gemini AI analysis (data sync only)",
    )
    args = parser.parse_args()
    use_ai = not args.no_ai

    load_dotenv()

    mode = "Full pipeline" if use_ai else "Data sync (no AI)"
    log.info("=== T212 Portfolio Checker — %s ===", mode)

    sheet = SheetManager()
    log.info("Sheet: %s", sheet.url)

    high_risk_items: list[str] = []

    # AI setup (only if needed)
    gemini = None
    budget = None
    if use_ai:
        from analyse import _get_client, AnalysisBudget
        gemini = _get_client()
        budget = AnalysisBudget()

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

    # ── Step 2: Market overview + Signal metrics → Signals tab ─────────────
    log.info("Step 2 — Computing market overview + signal metrics...")
    signals_summary = ""

    # 2a: Market overview (indices, VIX, yields, commodities)
    try:
        scorecard = get_market_scorecard()
        sheet.write_signals(scorecard, signal_type="Market")
        log.info("  %d market indicators fetched.", len(scorecard))
        for s in scorecard:
            val = s["value"]
            chg = s.get("change_pct", "")
            if isinstance(val, (int, float)) and isinstance(chg, (int, float)):
                log.info("    %-15s %10.2f  %+.2f%%  [%s]", s["name"], val, chg, s["signal"])
            else:
                log.info("    %-15s %10s         [%s]", s["name"], val, s["signal"])
    except Exception as e:
        log.error("  Market overview failed: %s", e)
        scorecard = []

    # 2b: High-reliability signal metrics
    try:
        signals = get_signal_metrics()
        sheet.write_signals(signals, signal_type="Signal")
        log.info("  %d signals computed.", len(signals))
        for s in signals:
            log.info("    %-25s %10s  [%s]", s["name"], s["value"], s["signal"])

        # Build combined summary for AI (market + signals)
        summary_lines = []
        for s in scorecard:
            val = s["value"]
            chg = s.get("change_pct", "")
            if isinstance(chg, (int, float)):
                summary_lines.append(f"{s['name']}: {val} ({chg:+.2f}%) [{s['signal']}]")
            else:
                summary_lines.append(f"{s['name']}: {val} [{s['signal']}]")
        for s in signals:
            summary_lines.append(f"{s['name']}: {s['value']} [{s['signal']}] ({s.get('reading', '')})")
        signals_summary = "\n".join(summary_lines)

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

    # 2c: AI Summary (based on market overview + signals) → Telegram if risky
    if use_ai and not budget.exhausted and signals_summary:
        log.info("Step 2c — AI Summary of market + signals...")
        from analyse import analyse_market_overview
        if budget.consume():
            try:
                ai_summary = analyse_market_overview(gemini, signals_summary, [])
                sheet.write_signal_ai_summary(ai_summary)
                log.info("  AI Summary: %s", ai_summary[:120])
                # Check if AI summary mentions high risk / bearish sentiment
                risk_keywords = ["high risk", "bearish", "sell-off", "crash", "correction",
                                 "recession", "extreme fear", "danger", "caution"]
                summary_lower = ai_summary.lower()
                if any(kw in summary_lower for kw in risk_keywords):
                    telegram_notify.notify_high_risk(
                        "AI Summary", ai_summary,
                    )
                    high_risk_items.append(f"AI Summary: {ai_summary[:100]}")
            except Exception as e:
                log.error("  AI Summary failed: %s", e)

    # ── Step 3: Fetch & sync portfolio from T212 pies ─────────────────────
    log.info("Step 3 — Fetching portfolio from Trading 212 (pies)...")
    positions = fetch_portfolio()
    if not positions:
        log.warning("  No positions returned. Check T212_API_KEY is correct.")
        log.warning("  Continuing with existing sheet data.")
    else:
        log.info("  %d positions fetched across pies. Getting live prices...", len(positions))
        for p in positions[:10]:
            log.info("    [%s] %-15s qty=%.4f  value=£%.2f  P/L=£%.2f",
                     p.get("pieName", "?")[:12], p["ticker"],
                     p["quantity"], p["value"], p["ppl"])
        if len(positions) > 10:
            log.info("    ... and %d more", len(positions) - 10)

        # Map T212 tickers to yfinance format for price + name lookup
        t212_tickers = [p["ticker"] for p in positions if p.get("ticker")]
        yf_tickers = [t212_to_yfinance(t) for t in t212_tickers]
        log.info("  Ticker mapping: %s", dict(list(zip(t212_tickers, yf_tickers))[:5]))
        yf_info = get_batch_info(yf_tickers)
        # Map prices and names back to T212 ticker keys
        live_prices = {}
        for t212_t, yf_t in zip(t212_tickers, yf_tickers):
            info = yf_info.get(yf_t, {})
            if info.get("price"):
                live_prices[t212_t] = info["price"]
            # Inject stock name from yfinance into position (T212 pies don't return names)
            if info.get("name"):
                for p in positions:
                    if p["ticker"] == t212_t and not p.get("name"):
                        p["name"] = info["name"]
        log.info("  Live prices fetched for %d/%d symbols.", len(live_prices), len(t212_tickers))
        sheet.sync_portfolio(positions, prices=live_prices)

    # ── Step 3.5: Stock Scanner ─────────────────────────────────────────────
    log.info("Step 3.5 — Running stock scanner...")
    try:
        from scanner import resolve_universe, run_scan
        scanner_conditions, universe_str = sheet.get_scanner_conditions()
        if scanner_conditions:
            universe = resolve_universe(universe_str)
            log.info("  %d conditions, universe: %s (%d tickers)",
                     len(scanner_conditions), universe_str or "US_MEGA50 (default)", len(universe))
            matches = run_scan(scanner_conditions, universe)
            sheet.write_scanner_results(matches)
            sheet.update_scanner_status(len(matches))
            if matches:
                telegram_notify.notify_scanner_matches(matches, len(matches))
                log.info("  %d matches written to Scanner Results.", len(matches))
            else:
                log.info("  No matches found.")
        else:
            log.info("  No scanner conditions configured. Skipping.")
    except Exception as e:
        log.error("  Scanner failed: %s", e)

    # ── Step 4: AI stock analysis (checkbox + stalest first, max 15) ──────
    if use_ai and not budget.exhausted:
        log.info("Step 4 — Analysing stocks (budget: %d remaining)...", budget.remaining)
        from analyse import analyse_stock
        stocks = sheet.get_portfolio_for_analysis(max_tickers=15)
        if not stocks:
            log.info("  No stocks have AI Analyse checked. Skipping.")
        else:
            log.info("  %d stocks queued for AI (sorted by stalest AI Updated).", len(stocks))
        analysed = 0
        for stock in stocks:
            if budget.exhausted:
                log.info("  Budget exhausted after %d stocks.", analysed)
                break
            symbol = stock["symbol"]
            yf_symbol = t212_to_yfinance(symbol)
            stale_info = stock.get("ai_updated", "never")
            log.info("  Analysing %s → %s (weight %s%%, last AI: %s)...",
                     symbol, yf_symbol, stock.get("weight", "0"), stale_info or "never")
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
    elif use_ai:
        log.info("Step 4 — Skipped (budget exhausted).")
    else:
        log.info("Step 4 — Skipped (--no-ai mode).")

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
    if use_ai:
        log.info(
            "=== Done — %d/%d Gemini requests used ===",
            budget.used, budget.max_requests,
        )
    else:
        log.info("=== Done — no AI requests used (--no-ai mode) ===")
    log.info("Sheet: %s", sheet.url)


if __name__ == "__main__":
    main()
