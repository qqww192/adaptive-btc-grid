"""
analyse.py
Sends the portfolio data to Google Gemini for analysis and returns a markdown report.

Uses the google-generativeai SDK with Search grounding for up-to-date market context.
"""

import os
import logging
from typing import Any

import google.generativeai as genai

log = logging.getLogger(__name__)

MODEL = "gemini-1.5-flash"


def _get_client() -> None:
    """Configure the Gemini SDK with the API key."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable is not set. "
            "See docs/setup.md for how to obtain and configure it."
        )
    genai.configure(api_key=api_key)


def _build_prompt(positions: list[dict[str, Any]], run_date: str) -> str:
    """Builds the analysis prompt from portfolio positions."""
    lines = [f"Portfolio snapshot as of {run_date}\n"]
    lines.append(f"{'Ticker':<12} {'Qty':>8} {'Avg Price':>10} {'Current':>10} {'P/L':>10}")
    lines.append("─" * 54)

    for pos in positions:
        ticker = pos.get("ticker", "N/A")
        qty = pos.get("quantity", 0)
        avg_price = pos.get("averagePrice", 0)
        current = pos.get("currentPrice", 0)
        ppl = pos.get("ppl", 0)
        lines.append(f"{ticker:<12} {qty:>8.2f} {avg_price:>10.2f} {current:>10.2f} {ppl:>10.2f}")

    portfolio_table = "\n".join(lines)

    prompt = f"""You are a financial analyst assistant. Analyse the following Trading 212 portfolio
and produce a concise markdown report.

{portfolio_table}

For each position, provide:
1. A brief assessment of the holding (1–2 sentences)
2. Key recent news or events affecting the stock
3. A risk rating from 1 (low) to 10 (high)

End with an overall portfolio summary including:
- Total diversification assessment
- Top risks across the portfolio
- Suggested actions (if any)

Format the output as clean markdown with headers and tables where appropriate.
"""
    return prompt


def analyse_portfolio(positions: list[dict[str, Any]], run_date: str) -> str:
    """
    Sends portfolio data to Gemini for analysis.
    Returns the analysis as a markdown string.
    """
    _get_client()

    prompt = _build_prompt(positions, run_date)

    model = genai.GenerativeModel(MODEL)
    response = model.generate_content(prompt)

    report = response.text
    log.info(f"Gemini analysis complete — {len(report)} chars.")
    return report
