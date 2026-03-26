"""
fetch_portfolio.py
Fetches all open positions and pie info from the Trading 212 API.

API docs: https://docs.trading212.com/api/positions
Endpoints:
  - GET /equity/positions — all open positions
  - GET /equity/pies      — all pies (name, id, instruments)

Auth: Basic Auth (API_KEY:SECRET_KEY base64-encoded) or legacy API key header.
Rate limit: respect the API's rate limits — never call in a tight loop.
"""

import base64
import json
import os
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://live.trading212.com/api/v0"
TIMEOUT = 30  # seconds


def _get_headers() -> dict[str, str]:
    """Build auth headers. Supports Basic Auth (key+secret) or legacy API key."""
    api_key = os.environ.get("T212_API_KEY", "")
    secret_key = os.environ.get("T212_SECRET_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "T212_API_KEY environment variable is not set. "
            "See docs/setup.md for how to obtain and configure it."
        )
    if secret_key:
        # Recommended: Basic Auth with API key + secret
        credentials = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
        return {"Authorization": f"Basic {credentials}"}
    else:
        # Legacy: API key only
        return {"Authorization": api_key}


def _extract_ticker(pos: dict) -> str:
    """
    Extract ticker from a T212 position dict.
    Handles multiple possible field names across API versions.
    """
    # Try known field names in order of likelihood
    for field in ("ticker", "code", "instrumentCode", "instrument"):
        val = pos.get(field)
        if val and isinstance(val, str):
            return val
    # If instrument is a dict (newer API), try nested fields
    instrument = pos.get("instrument")
    if isinstance(instrument, dict):
        for field in ("ticker", "code", "symbol", "name"):
            val = instrument.get(field)
            if val:
                return val
    return "UNKNOWN"


def _normalise_position(raw: dict) -> dict:
    """
    Normalise a T212 position into a consistent format regardless of API version.
    Returns: {ticker, quantity, averagePrice, currentPrice, ppl, fxPpl, pieQuantity}
    """
    ticker = _extract_ticker(raw)

    # Try multiple field names for each value
    qty = (raw.get("quantity")
           or raw.get("qty")
           or 0)
    avg_price = (raw.get("averagePrice")
                 or raw.get("averagePricePaid")
                 or raw.get("avgPrice")
                 or 0)
    current_price = (raw.get("currentPrice")
                     or raw.get("price")
                     or 0)
    ppl = (raw.get("ppl")
           or raw.get("unrealizedPl")
           or raw.get("profitLoss")
           or 0)
    fx_ppl = raw.get("fxPpl", 0)
    pie_qty = (raw.get("pieQuantity")
               or raw.get("quantityInPies")
               or 0)

    return {
        "ticker": ticker,
        "quantity": float(qty),
        "averagePrice": float(avg_price),
        "currentPrice": float(current_price),
        "ppl": float(ppl),
        "fxPpl": float(fx_ppl or 0),
        "pieQuantity": float(pie_qty or 0),
    }


def fetch_all_positions() -> list[dict[str, Any]]:
    """
    Fetches all open positions from Trading 212.

    Returns a list of normalised position dicts:
      {ticker, quantity, averagePrice, currentPrice, ppl, fxPpl, pieQuantity}
    """
    headers = _get_headers()

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{BASE_URL}/equity/positions",
                headers=headers,
            )
            resp.raise_for_status()
            raw_positions = resp.json()

        log.info("Fetched %d positions from Trading 212.", len(raw_positions))

        # Dump full raw JSON for first position to debug field names
        if raw_positions:
            log.info("T212 RAW FIELDS (first position): %s",
                     json.dumps(raw_positions[0], default=str))
            if len(raw_positions) > 1:
                log.info("T212 RAW FIELDS (second position): %s",
                         json.dumps(raw_positions[1], default=str))

        # Normalise all positions
        positions = [_normalise_position(p) for p in raw_positions]

        for p in positions[:5]:
            log.info("  Position: %s qty=%.4f avg=%.2f price=%.2f ppl=%.2f",
                     p["ticker"], p["quantity"], p["averagePrice"],
                     p["currentPrice"], p["ppl"])

        return positions

    except httpx.HTTPStatusError as e:
        log.error("Trading 212 API error: %s — %s", e.response.status_code, e.response.text)
        raise
    except httpx.RequestError as e:
        log.error("Network error contacting Trading 212: %s", e)
        raise


def fetch_pies() -> list[dict[str, Any]]:
    """
    Fetches all pies from Trading 212.
    Returns a list of pie dicts with id, name, instruments, etc.
    """
    headers = _get_headers()

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{BASE_URL}/equity/pies",
                headers=headers,
            )
            resp.raise_for_status()
            pies = resp.json()

        log.info("Fetched %d pies from Trading 212.", len(pies))
        # Dump full raw JSON for first pie
        if pies:
            log.info("T212 RAW PIE (first): %s",
                     json.dumps(pies[0], default=str))
        for pie in pies:
            log.info("  Pie: id=%s", pie.get("id"))
        return pies

    except httpx.HTTPStatusError as e:
        log.error("Trading 212 pies API error: %s — %s", e.response.status_code, e.response.text)
        return []
    except httpx.RequestError as e:
        log.error("Network error fetching pies: %s", e)
        return []


def t212_to_yfinance(t212_ticker: str) -> str:
    """
    Convert T212 ticker format to yfinance ticker format.

    T212 uses: VUAG_EQ_L, AAPL_US_EQ, CSPX_EQ_L, etc.
    yfinance uses: VUAG.L, AAPL, CSPX.L, etc.

    Exchange suffixes:
      _L  → .L (London)
      _US → (no suffix, US default)
      _DE → .DE (Frankfurt)
      _AS → .AS (Amsterdam)
      _PA → .PA (Paris)
      _MI → .MI (Milan)
    """
    if not t212_ticker or t212_ticker == "UNKNOWN":
        return t212_ticker

    # Strip common T212 suffixes
    parts = t212_ticker.split("_")
    symbol = parts[0]

    # Map T212 exchange codes to yfinance suffixes
    exchange_map = {
        "L": ".L",
        "US": "",
        "DE": ".DE",
        "AS": ".AS",
        "PA": ".PA",
        "MI": ".MI",
        "MC": ".MC",
        "SW": ".SW",
        "TO": ".TO",
        "HK": ".HK",
    }

    # Find exchange code in the parts
    for part in parts[1:]:
        if part in exchange_map:
            return f"{symbol}{exchange_map[part]}"

    # If the last part looks like an exchange code
    if len(parts) > 1 and parts[-1] in exchange_map:
        return f"{symbol}{exchange_map[parts[-1]]}"

    # Default: just the base symbol
    return symbol
