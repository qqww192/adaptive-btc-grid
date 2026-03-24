"""
news_alert.py
Daily news scanner for ETF-focused portfolio.

Uses Google Gemini (gemini-2.0-flash) with Search grounding enabled —
Gemini fetches live financial news directly without needing a separate
web search tool call.

Workflow:
  1. Fetch current ETF tickers from T212
  2. Ask Gemini to search and score today's market news
  3. Only Tier 1 (score 8–10, surprise macro/crash/fund events) triggers Telegram
  4. All tiers are logged to Google Doc as an audit trail

Threshold rationale:
  ETFs diversify away company noise. Same-day action is only warranted for
  genuine macro shocks, surprise rate decisions, or fund-level incidents.
  Tier 2 (5–7) context surfaces in the weekly report instead.
"""

import os
import json
import logging
import urllib.request
import urllib.error
from typing import Any

import google.generativeai as genai
from google.generativeai.types import Tool, GenerateContentConfig
from google.generativeai import protos

log = logging.getLogger(__name__)

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-2.0-flash"
SEARCH_TOOL = Tool(google_search=protos.GoogleSearch())

# ── Telegram config ───────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

TEMPERATURE_EMOJI = {
    "calm": "🟢",
    "cautious": "🟡",
    "elevated": "🟠",
    "high alert": "🔴",
}

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_INSTRUCTION = """
You are a market intelligence analyst specialising in ETF portfolios.
Scan today's financial news and identify stories requiring immediate investor action.

Investor profile: holds ETFs, base currency GBP, long-term passive strategy.

## Scoring — be very strict. Grade inflation causes alert fatigue.

Score 8–10 (Tier 1 — ALERT, same-day action needed):
- Central bank decisions WITH A SURPRISE: unexpected cut/hike, emergency inter-meeting move.
  A consensus hold that 95% of economists predicted = score 6, NOT Tier 1.
- Major index crash: S&P 500 / FTSE 100 / MSCI World down >3% intraday, circuit breakers triggered.
- An ETF the investor holds: suspended, liquidated, or major NAV pricing error.
- Geopolitical black swan with immediate market impact (exchange closure risk, new war front).
- Emergency regulatory action suspending a major ETF provider (Vanguard, iShares, SPDR, HSBC).

Score 5–7 (Tier 2 — log only, surface in weekly report):
- Moderate CPI/GDP surprise (>0.2% miss/beat vs consensus).
- Index rebalances affecting ETF composition.
- Expense ratio or fund structure changes.
- ETF moves 2–3% without obvious catalyst.
- GBP >1% single-session move.

Score 1–4 (Tier 3 — brief log, no further action):
- Analyst opinions, price target changes.
- Individual stock earnings within an ETF.
- Routine "markets were mixed" commentary.
- Speculation or predictions.
- News older than 48 hours.

## The key test
Ask: "Does this ETF investor need to DO something TODAY?"
If no — or if the event was fully expected — score below 8.

## Output — return ONLY valid JSON, no markdown fences, no preamble

{
  "scan_date": "YYYY-MM-DD",
  "etfs_analysed": ["TICKER1"],
  "tier1_alerts": [
    {
      "score": 9,
      "headline": "Concise headline (max 80 chars)",
      "what_happened": "One sentence with numbers where possible.",
      "why_it_matters": "One sentence on direct ETF valuation impact.",
      "affected_etfs": ["TICKER1"],
      "action_suggested": "One concrete sentence."
    }
  ],
  "tier2_log": [
    { "score": 6, "headline": "Brief headline", "note": "One sentence on relevance" }
  ],
  "tier3_log": [
    { "headline": "Brief headline", "reason": "Why scored below 5" }
  ],
  "overall_market_temperature": "calm | cautious | elevated | high alert",
  "one_line_summary": "One honest sentence on today's macro backdrop."
}
"""


# ── Portfolio helpers ─────────────────────────────────────────────────────────

def _extract_etf_tickers(positions: list[dict[str, Any]]) -> list[str]:
    """Strips T212 exchange suffixes (VWRL_EQ -> VWRL) and deduplicates."""
    tickers = []
    for p in positions:
        ticker = p.get("ticker", "")
        clean = ticker.split("_")[0] if "_" in ticker else ticker
        if clean:
            tickers.append(clean)
    return list(dict.fromkeys(tickers))


# ── News scan ─────────────────────────────────────────────────────────────────

def scan_for_alerts(positions: list[dict[str, Any]], scan_date: str) -> dict:
    """
    Asks Gemini (with Search grounding) to scan today's news and score stories.
    Returns structured dict parsed from Gemini's JSON response.
    """
    tickers = _extract_etf_tickers(positions)
    ticker_list = ", ".join(tickers)

    prompt = f"""
Today is {scan_date}. The investor holds these ETFs: {ticker_list}

Search today's financial news thoroughly. Look for:
- Central bank rate decisions (Fed, BoE, ECB, BoJ) — note whether they surprised markets
- Major index moves on FTSE 100, S&P 500, MSCI World, NASDAQ
- Any news about these specific ETF tickers: {ticker_list}
- GBP/USD exchange rate significant moves
- CPI, GDP, or jobs data releases vs consensus
- Any ETF suspensions or fund-level announcements

Score each story strictly. A priced-in consensus hold = 6 max.
Return ONLY the JSON object — no markdown fences, no preamble.
""".strip()

    log.info(f"Scanning news for {len(tickers)} ETFs: {ticker_list}")

    model = genai.GenerativeModel(
        model_name=MODEL,
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[SEARCH_TOOL],
    )

    response = model.generate_content(
        prompt,
        generation_config=GenerateContentConfig(
            temperature=0.1,        # very low — we need consistent JSON output
            max_output_tokens=3000,
        ),
    )

    raw = (
        response.text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}\nRaw (first 500): {raw[:500]}")
        return {
            "scan_date": scan_date,
            "etfs_analysed": tickers,
            "tier1_alerts": [],
            "tier2_log": [],
            "tier3_log": [],
            "overall_market_temperature": "unknown",
            "one_line_summary": "News scan failed — JSON parse error. Check Actions logs.",
        }


# ── Telegram delivery ─────────────────────────────────────────────────────────

def _send_telegram_message(text: str) -> None:
    """Sends a message via Telegram Bot API using stdlib only."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram secrets not set — printing alert to stdout:\n" + text)
        print(text)
        return

    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4090],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            if not body.get("ok"):
                log.error(f"Telegram API error: {body}")
            else:
                log.info("Telegram message delivered.")
    except urllib.error.URLError as e:
        log.error(f"Telegram unreachable: {e}")
        raise


def _format_tier1_message(alert: dict, scan_date: str) -> str:
    score = alert.get("score", "?")
    etfs = ", ".join(alert.get("affected_etfs", []))
    return (
        f"🔴 <b>PORTFOLIO ALERT — {scan_date}</b>\n\n"
        f"<b>{alert.get('headline', '')}</b>\n\n"
        f"📌 <b>What happened:</b> {alert.get('what_happened', '')}\n\n"
        f"📉 <b>Why it matters:</b> {alert.get('why_it_matters', '')}\n\n"
        f"🎯 <b>Affected ETFs:</b> <code>{etfs}</code>\n\n"
        f"💡 <b>Suggested action:</b> {alert.get('action_suggested', '')}\n\n"
        f"<i>Score: {score}/10 · Not financial advice</i>"
    )


def _format_quiet_summary(result: dict, scan_date: str) -> str:
    temp = result.get("overall_market_temperature", "unknown")
    summary = result.get("one_line_summary", "")
    temp_emoji = TEMPERATURE_EMOJI.get(temp, "⚪")
    tier2_count = len(result.get("tier2_log", []))
    tier2_note = (
        f"\n\n📋 <i>{tier2_count} notable item(s) logged for your weekly report.</i>"
        if tier2_count else ""
    )
    return (
        f"{temp_emoji} <b>Daily scan — {scan_date}</b>\n"
        f"{summary}"
        f"{tier2_note}\n\n"
        f"<i>No high-impact alerts today.</i>"
    )


def send_telegram_alerts(result: dict, scan_date: str) -> bool:
    """
    Sends Tier 1 alerts to Telegram; quiet summary on calm days.
    Returns True if at least one Tier 1 alert was sent.
    """
    tier1 = result.get("tier1_alerts", [])
    if tier1:
        for alert in tier1:
            _send_telegram_message(_format_tier1_message(alert, scan_date))
        log.info(f"Sent {len(tier1)} Tier 1 Telegram alert(s).")
        return True
    else:
        _send_telegram_message(_format_quiet_summary(result, scan_date))
        log.info("No Tier 1 alerts — quiet summary sent.")
        return False


# ── Google Doc formatter ──────────────────────────────────────────────────────

def format_alert_for_doc(result: dict) -> str:
    """Full audit trail entry — all tiers — formatted as Markdown."""
    scan_date = result.get("scan_date", "unknown")
    temp = result.get("overall_market_temperature", "unknown")
    tier1 = result.get("tier1_alerts", [])
    tier2 = result.get("tier2_log", [])
    tier3 = result.get("tier3_log", [])

    lines = [
        f"## 📡 News Scan — {scan_date}",
        f"**Market temperature:** {temp.title()}",
        f"**Summary:** {result.get('one_line_summary', '')}",
        "",
    ]

    if tier1:
        lines.append("### 🔴 Tier 1 — Telegram alerts sent")
        for a in tier1:
            lines.append(f"- **{a.get('headline', '')}** (score {a.get('score', '?')}/10)")
            lines.append(f"  _{a.get('what_happened', '')} {a.get('why_it_matters', '')}_")
            lines.append(f"  Action: {a.get('action_suggested', '')}")
    else:
        lines.append("### ✅ No Tier 1 alerts — quiet summary sent")

    if tier2:
        lines.append("")
        lines.append("### 🟡 Tier 2 — notable, in weekly report")
        for a in tier2:
            lines.append(f"- (score {a.get('score', '?')}/10) {a.get('headline', '')} — {a.get('note', '')}")

    if tier3:
        lines.append("")
        lines.append("### ⚪ Tier 3 — monitored, not relevant")
        for a in tier3:
            lines.append(f"- {a.get('headline', '')} — {a.get('reason', '')}")

    return "\n".join(lines)
