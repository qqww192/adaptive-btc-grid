"""
analyse.py
Three-tier Gemini analysis engine with request budget management.

Tiers (run in order, budget = 19 requests/day):
  1. Watchlist  — analyse user-added symbols (1 request each)
  2. Basic      — market overview by region/sector weighted by portfolio (1 request)
  3. Advanced   — deep individual stock research, prioritised by sheet (1 request each)

Uses the google-genai SDK. Each call counts against the daily budget.
"""

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


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable is not set. "
            "See docs/setup.md for configuration."
        )
    return genai.Client(api_key=api_key)


def _call_gemini(client: genai.Client, prompt: str) -> str:
    """Call Gemini with retry on 429. Returns the response text."""
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
                log.warning(
                    "Gemini rate-limited (attempt %d/%d). Waiting %.0fs.",
                    attempt, max_retries, wait,
                )
                time.sleep(wait)
            else:
                raise

    text = response.text
    if not text:
        log.error("Gemini returned no text. Full response: %s", response)
        raise RuntimeError("Gemini returned an empty response.")
    return text


# ── Tier 1: Watchlist Analysis ─────────────────────────────────────────────


def analyse_watchlist_symbol(client: genai.Client, symbol: str, market: str) -> str:
    """
    Deep research on a watchlist symbol using multi-layered analysis.
    Inspired by quantitative research methodology:
    1. Fundamental snapshot
    2. Technical momentum
    3. Catalyst / news scan
    4. Risk assessment
    5. Valuation verdict
    """
    prompt = f"""You are an expert equity research analyst. Perform a comprehensive
analysis of {symbol} ({market} market).

Structure your analysis as follows:

## 1. Company & Fundamental Snapshot
- Business model, revenue drivers, competitive moat
- Latest earnings highlights (EPS, revenue growth, margins)
- Balance sheet health (debt/equity, cash position)

## 2. Technical & Momentum Analysis
- Current price trend (bullish/bearish/neutral)
- Key support and resistance levels
- Volume trends and any notable patterns

## 3. Catalyst & News Scan
- Recent material news, earnings surprises, or guidance changes
- Upcoming catalysts (earnings dates, product launches, regulatory)
- Sector/industry tailwinds or headwinds

## 4. Risk Assessment
- Top 3 risks specific to this stock
- Macro risks (interest rates, geopolitical, currency)
- Risk rating: 1 (very low) to 10 (very high)

## 5. Valuation Verdict
- Fair value estimate vs current price
- Bull case / Base case / Bear case price targets
- Overall rating: STRONG BUY / BUY / HOLD / SELL / STRONG SELL

Be concise but thorough. Use specific numbers where possible.
Format as clean markdown.
"""
    return _call_gemini(client, prompt)


# ── Tier 2: Basic Market Overview ──────────────────────────────────────────


def analyse_market_overview(
    client: genai.Client,
    positions: list[dict[str, Any]],
) -> list[dict]:
    """
    Analyse major market indices and sectors weighted by portfolio exposure.
    Returns a list of {region, index_sector, weight, analysis} dicts.
    """
    # Build portfolio weight summary
    regions: dict[str, float] = {}
    sectors: dict[str, list[str]] = {}
    for pos in positions:
        ticker = pos.get("ticker", "N/A")
        market = pos.get("market", _detect_market_from_ticker(ticker))
        value = abs(float(pos.get("currentPrice", 0)) * float(pos.get("quantity", 0)))
        regions[market] = regions.get(market, 0) + value

    total = sum(regions.values()) or 1
    weight_summary = "\n".join(
        f"- {region}: {val / total * 100:.1f}% of portfolio"
        for region, val in sorted(regions.items(), key=lambda x: -x[1])
    )

    ticker_list = ", ".join(
        pos.get("ticker", "N/A") for pos in positions[:60]
    )

    prompt = f"""You are a macro strategist. Analyse the current market environment
relevant to this portfolio:

Portfolio regional weights:
{weight_summary}

Holdings: {ticker_list}

Provide a concise daily market briefing covering:

## US Market
- S&P 500, NASDAQ, Dow Jones: current momentum (bullish/bearish/neutral)
- Key sector performance (tech, healthcare, financials, energy, etc.)
- Notable macro signals (Fed, yields, VIX, DXY)

## UK Market
- FTSE 100, FTSE 250: current momentum
- Key sector performance
- Notable macro signals (BoE, gilts, GBP)

## Global Signals
- Major international indices (DAX, Nikkei, Hang Seng)
- Commodities (oil, gold, copper) trends
- Risk-on vs risk-off sentiment

## Portfolio Impact
- Which of my holdings benefit from current conditions
- Which face headwinds
- Key events to watch this week

Keep it actionable and concise. Use bullet points.
Format each region as a separate section.
"""
    full_analysis = _call_gemini(client, prompt)

    # Parse into structured entries for the sheet
    entries = []
    for region in ["US", "UK", "Global"]:
        weight_pct = f"{regions.get(region, 0) / total * 100:.1f}%" if region in regions else "N/A"
        entries.append({
            "region": region,
            "index_sector": f"{region} Market Overview",
            "weight": weight_pct,
            "analysis": full_analysis if region == "US" else "",  # Full analysis on first entry
        })

    # Put the full analysis only on the first entry to avoid duplication
    entries[0]["analysis"] = full_analysis

    return entries


# ── Tier 3: Advanced Individual Stock Analysis ─────────────────────────────


def analyse_stock_advanced(
    client: genai.Client,
    symbol: str,
    market: str,
    quantity: Any,
    avg_price: Any,
    current_price: Any,
    ppl: Any,
) -> str:
    """
    Deep research on a portfolio stock with position context.
    Uses 8-layer research methodology:
    1. Business quality & moat
    2. Financial health deep-dive
    3. Growth trajectory
    4. Management & governance
    5. Technical analysis
    6. Sentiment & flow
    7. Risk matrix
    8. Position-specific advice
    """
    prompt = f"""You are a senior equity research analyst conducting a deep-dive on
{symbol} ({market} market).

My position: {quantity} shares, avg price {avg_price}, current {current_price},
P/L: {ppl}

Conduct an 8-layer research analysis:

## 1. Business Quality & Moat
- Core business model and competitive advantages
- Market position and barriers to entry
- Moat durability rating (wide/narrow/none)

## 2. Financial Health
- Revenue growth (YoY, QoQ trends)
- Profit margins (gross, operating, net) and trends
- Free cash flow generation and capital allocation
- Debt levels and interest coverage

## 3. Growth Trajectory
- TAM (total addressable market) and penetration
- Growth drivers for next 1-3 years
- Consensus revenue/EPS growth estimates

## 4. Management & Governance
- Management track record and insider ownership
- Capital allocation history (buybacks, dividends, M&A)
- Any red flags or concerns

## 5. Technical Analysis
- Price trend and key levels (support/resistance)
- Moving average signals (50/200 DMA)
- RSI and momentum indicators

## 6. Sentiment & Flow
- Institutional ownership changes
- Short interest and days to cover
- Analyst consensus and recent rating changes

## 7. Risk Matrix
- Company-specific risks (top 3)
- Sector risks
- Macro risks
- Overall risk score: 1-10

## 8. Position Advice
- Given my entry at {avg_price} and current P/L of {ppl}:
  - Hold / Add / Trim / Exit recommendation
  - Suggested stop-loss level
  - Price targets (6-month, 12-month)

Overall verdict: STRONG BUY / BUY / HOLD / SELL / STRONG SELL

Be specific with numbers. Format as clean, concise markdown.
"""
    return _call_gemini(client, prompt)


# ── Budget-aware orchestrator ──────────────────────────────────────────────


class AnalysisBudget:
    """Tracks Gemini API request budget for a single run."""

    def __init__(self, max_requests: int = MAX_DAILY_REQUESTS):
        self.max_requests = max_requests
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.max_requests - self.used

    def consume(self) -> bool:
        """Consume one request. Returns True if budget allows, False if exhausted."""
        if self.used >= self.max_requests:
            return False
        self.used += 1
        log.info("Gemini request %d/%d used.", self.used, self.max_requests)
        return True

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_requests


def _detect_market_from_ticker(ticker: str) -> str:
    if ticker.endswith("_EQ") or ".L" in ticker or ticker.endswith("_LSE"):
        return "UK"
    return "US"
