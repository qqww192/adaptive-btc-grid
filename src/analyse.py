"""
analyse.py
Gemini analysis engine — returns structured data, not markdown blobs.

Each stock analysis returns: verdict, fair_value, risk, key_note
Market overview returns: a short summary sentence for the sheet.
"""

import json
import os
import re
import logging
import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors

log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
MAX_DAILY_REQUESTS = 19
RPM_LIMIT = 5
_MIN_INTERVAL = 60.0 / RPM_LIMIT  # 12 seconds between calls
_last_call_time: float = 0.0


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable is not set. "
            "See docs/setup.md for configuration."
        )
    return genai.Client(api_key=api_key)


def _call_gemini(client: genai.Client, prompt: str) -> str:
    """Call Gemini with RPM throttling and retry on 429."""
    global _last_call_time
    now = time.time()
    elapsed = now - _last_call_time
    if elapsed < _MIN_INTERVAL:
        wait = _MIN_INTERVAL - elapsed
        log.info("RPM throttle: waiting %.1fs before next Gemini call.", wait)
        time.sleep(wait)
    _last_call_time = time.time()

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(model=MODEL, contents=prompt)
            break
        except genai_errors.ClientError as exc:
            if exc.code == 429 and attempt < max_retries:
                wait = 60 * attempt
                match = re.search(r"retry in ([\d.]+)s", str(exc), re.IGNORECASE)
                if match:
                    wait = float(match.group(1)) + 2
                log.warning("Rate-limited (attempt %d/%d). Waiting %.0fs.", attempt, max_retries, wait)
                time.sleep(wait)
            else:
                raise

    text = response.text
    if not text:
        raise RuntimeError("Gemini returned an empty response.")
    return text


def _parse_json_response(text: str) -> dict:
    """Extract JSON from Gemini response (handles markdown code blocks)."""
    # Try to find JSON in code block
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(1))
    # Try raw JSON
    brace_start = text.find("{")
    brace_end = text.rfind("}") + 1
    if brace_start >= 0 and brace_end > brace_start:
        return json.loads(text[brace_start:brace_end])
    raise ValueError(f"No JSON found in response: {text[:200]}")


# ── Stock Analysis (structured) ──────────────────────────────────────────────


def analyse_stock(
    client: genai.Client,
    symbol: str,
    financial_context: str,
    amount: Any,
    price: Any,
    weight: Any,
    market_context: str = "",
) -> dict:
    """
    Analyse a stock and return structured data.
    Returns: {verdict, fair_value, risk, key_note}
    """
    prompt = f"""You are a senior equity analyst. Analyse {symbol} and return ONLY a JSON object.

Live data: {financial_context}
Position: {amount} shares at ${price}, weight: {weight}%
Market context: {market_context}

Criteria to evaluate:
- P/E vs industry average (undervalued if below)
- Revenue growth consistency
- Profit margin trend
- Debt-to-equity vs peers
- Sentiment vs fundamentals gap (is market mispricing it?)
- Free cash flow strength

Return ONLY this JSON (no other text):
{{
  "verdict": "STRONG BUY" or "BUY" or "HOLD" or "SELL" or "STRONG SELL",
  "fair_value": estimated fair value as a number (e.g. 185.50),
  "risk": risk score 1-10 (1=very safe, 10=very risky),
  "key_note": "One sentence: why this verdict, mention the key number that matters most"
}}"""

    text = _call_gemini(client, prompt)
    try:
        result = _parse_json_response(text)
        return {
            "verdict": str(result.get("verdict", "HOLD")),
            "fair_value": str(result.get("fair_value", "")),
            "risk": str(result.get("risk", "5")),
            "key_note": str(result.get("key_note", "")),
        }
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse JSON for %s, using fallback: %s", symbol, e)
        return {
            "verdict": "HOLD",
            "fair_value": "",
            "risk": "5",
            "key_note": text[:150].replace("\n", " "),
        }


# ── Market Overview ──────────────────────────────────────────────────────────


def analyse_market_overview(
    client: genai.Client,
    market_summary: str,
    positions: list[dict[str, Any]],
) -> str:
    """
    Brief market interpretation. Returns a single sentence for the sheet.
    The actual data (VIX, yields, etc.) is already in the sheet from yfinance.
    This just adds the AI interpretation.
    """
    ticker_list = ", ".join(pos.get("ticker", "N/A") for pos in positions[:30])

    prompt = f"""You are a macro strategist. Given this market data, write ONE sentence (max 30 words) summarising the overall market stance and what it means for a portfolio holding: {ticker_list}

Market data:
{market_summary}

Return ONLY the sentence, no quotes or extra text."""

    return _call_gemini(client, prompt).strip().strip('"')


# ── Budget ────────────────────────────────────────────────────────────────────


class AnalysisBudget:
    """Tracks Gemini API request budget for a single run."""

    def __init__(self, max_requests: int = MAX_DAILY_REQUESTS):
        self.max_requests = max_requests
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.max_requests - self.used

    def consume(self) -> bool:
        if self.used >= self.max_requests:
            return False
        self.used += 1
        log.info("Gemini request %d/%d used.", self.used, self.max_requests)
        return True

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_requests
