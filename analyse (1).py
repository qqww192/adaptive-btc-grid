"""
analyse.py
Sends portfolio positions to Google Gemini (gemini-2.0-flash) for analysis.
Uses the google-generativeai SDK with grounding (Google Search) enabled
so Gemini can look up live prices and recent news per ticker.
Returns a formatted Markdown report string.
"""

import os
import logging
from typing import Any

import google.generativeai as genai
from google.generativeai.types import Tool, GenerateContentConfig
from google.generativeai import protos

log = logging.getLogger(__name__)

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

MODEL = "gemini-2.0-flash"

# Google Search grounding — lets Gemini fetch live data
SEARCH_TOOL = Tool(google_search=protos.GoogleSearch())


def _build_portfolio_table(positions: list[dict[str, Any]]) -> str:
    """Converts raw T212 positions into a Markdown table for the prompt."""
    lines = [
        "| Ticker | Qty | Avg Price | Current Price | P&L (£) | P&L % |",
        "|--------|-----|-----------|--------------|---------|-------|",
    ]
    for p in positions:
        ticker = p.get("ticker", "?")
        qty = p.get("quantity", 0)
        avg = p.get("averagePrice", 0)
        current = p.get("currentPrice", 0)
        pnl = p.get("ppl", 0)
        pnl_pct = ((current - avg) / avg * 100) if avg else 0
        lines.append(
            f"| {ticker} | {qty} | {avg:.2f} | {current:.2f} | {pnl:+.2f} | {pnl_pct:+.1f}% |"
        )
    return "\n".join(lines)


SYSTEM_INSTRUCTION = """
You are a sharp, candid portfolio analyst. Review this retail investor's Trading 212 portfolio
and produce a concise, actionable Markdown report.

Investor profile:
- Base currency: GBP
- Strategy: long-term, dividend-focused where possible
- Risk tolerance: moderate
- Horizon: 5–10 years

Use Google Search to check for recent news, earnings, and dividend changes on the tickers
before writing your analysis. Focus especially on any position with >10% unrealised loss.

Your report must follow this structure exactly:

## 📊 Portfolio Report — {date}

### Executive Summary
[3–4 sentences: overall health, biggest risk, biggest opportunity]

### Position Review
[One bullet per position: ticker — status (Hold / Watch / Consider trimming) — one-line reason]

### Top Risks Right Now
[3 bullet points maximum — be specific, not generic]

### Opportunities to Consider
[2–3 ideas: existing positions to add to, or new instruments that fit the strategy]

### Macro Tailwinds & Headwinds
[Brief: interest rates, GBP/USD FX, any relevant sector macro]

### Suggested Actions Before Next Review
[Numbered list, max 5 items — concrete and prioritised]

---
*AI-generated analysis for informational purposes only. Not financial advice.*

Rules:
- Be direct. Avoid filler phrases.
- Use £ for monetary values.
- Flag any position with >20% unrealised loss as a priority Watch.
- Keep the whole report under 800 words.
"""


def analyse_portfolio(positions: list[dict[str, Any]], run_date: str) -> str:
    """
    Sends the portfolio to Gemini and returns a Markdown report string.
    Gemini Search grounding is enabled so it can retrieve live market data.
    """
    table = _build_portfolio_table(positions)
    total_pnl = sum(p.get("ppl", 0) for p in positions)
    total_invested = sum(
        p.get("quantity", 0) * p.get("averagePrice", 0) for p in positions
    )

    prompt = f"""
Analyse my Trading 212 portfolio as of {run_date}.

Portfolio overview:
- Total positions: {len(positions)}
- Total invested (approx): £{total_invested:,.2f}
- Total unrealised P&L: £{total_pnl:+,.2f}

Position detail:
{table}

Search for recent news (last 30 days) on any positions showing significant moves
before writing the report. Follow the report structure in your instructions exactly.
""".strip()

    log.info(f"Sending {len(positions)} positions to Gemini for analysis...")

    model = genai.GenerativeModel(
        model_name=MODEL,
        system_instruction=SYSTEM_INSTRUCTION.replace("{date}", run_date),
        tools=[SEARCH_TOOL],
    )

    response = model.generate_content(
        prompt,
        generation_config=GenerateContentConfig(
            temperature=0.3,       # lower = more consistent, factual tone
            max_output_tokens=2000,
        ),
    )

    report = response.text.strip()
    log.info(f"Analysis received — {len(report)} characters.")
    return report
