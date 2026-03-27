"""
fetch_portfolio.py
Fetches all open positions and pie info from the Trading 212 API.

API docs: https://docs.trading212.com/api/positions
Endpoints:
  - GET /equity/positions — all open positions
  - GET /equity/pies      — all pies (name, id, instruments)

T212 Position response format (camelCase):
  {
    "instrument": {"currency": "GBX", "isin": "...", "name": "...", "ticker": "VUAG_EQ_L"},
    "quantity": 5.138,
    "averagePricePaid": 247.50,
    "currentPrice": 24781.35,        # in instrument currency (e.g. GBX = pence)
    "quantityAvailableForTrading": 5.138,
    "quantityInPies": 5.138,
    "createdAt": "2024-...",
    "walletImpact": {
      "currency": "GBP",
      "currentValue": 1273.26,        # in account currency
      "fxImpact": 0,
      "totalCost": 1208.46,
      "unrealizedProfitLoss": 64.80
    }
  }

Note: London-listed instruments may be priced in GBX (pence). We convert to GBP.

Auth: Basic Auth (API_KEY:SECRET_KEY base64-encoded) or legacy API key header.
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

# Currencies where price is in minor units (pence, cents)
# and needs dividing by 100 to get the major unit (GBP, etc.)
MINOR_CURRENCY_CODES = {"GBX", "GBp", "ILA"}  # GBX=pence, ILA=Israeli agora


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
        credentials = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
        return {"Authorization": f"Basic {credentials}"}
    else:
        return {"Authorization": api_key}


def _normalise_position(raw: dict) -> dict:
    """
    Normalise a T212 API position into a flat, consistent format.

    Handles the nested response structure:
      - instrument.ticker → ticker
      - instrument.name → name
      - instrument.currency → currency
      - averagePricePaid → averagePrice
      - walletImpact.unrealizedProfitLoss → ppl
      - walletImpact.currentValue → value (in account currency)
    """
    # Extract instrument info (nested object)
    instrument = raw.get("instrument") or {}
    ticker = instrument.get("ticker", "")
    name = instrument.get("name", "")
    currency = instrument.get("currency", "")
    isin = instrument.get("isin", "")

    # Core position data
    qty = float(raw.get("quantity") or 0)
    avg_price = float(raw.get("averagePricePaid") or 0)
    current_price = float(raw.get("currentPrice") or 0)
    qty_in_pies = float(raw.get("quantityInPies") or 0)

    # Convert minor currency (GBX pence) to major currency (GBP)
    if currency.upper() in MINOR_CURRENCY_CODES:
        avg_price = avg_price / 100
        current_price = current_price / 100

    # Wallet impact (P/L, value in account currency)
    wallet = raw.get("walletImpact") or {}
    ppl = float(wallet.get("unrealizedProfitLoss") or 0)
    value = float(wallet.get("currentValue") or 0)
    total_cost = float(wallet.get("totalCost") or 0)
    fx_impact = float(wallet.get("fxImpact") or 0)
    account_currency = wallet.get("currency", "GBP")

    return {
        "ticker": ticker,
        "name": name,
        "isin": isin,
        "currency": currency,
        "accountCurrency": account_currency,
        "quantity": qty,
        "averagePrice": avg_price,
        "currentPrice": current_price,
        "ppl": ppl,
        "fxImpact": fx_impact,
        "value": value,
        "totalCost": total_cost,
        "quantityInPies": qty_in_pies,
    }


def fetch_all_positions() -> list[dict[str, Any]]:
    """
    Fetches all open positions from Trading 212.

    Returns a list of normalised position dicts with consistent field names.
    """
    headers = _get_headers()

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{BASE_URL}/equity/positions",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

        # Handle both array response and paginated {items: [...]} response
        if isinstance(data, list):
            raw_positions = data
        elif isinstance(data, dict):
            raw_positions = data.get("items") or data.get("positions") or [data]
            log.info("T212 response is dict with keys: %s", list(data.keys()))
        else:
            log.error("Unexpected T212 response type: %s", type(data))
            return []

        log.info("Fetched %d positions from Trading 212.", len(raw_positions))

        # Dump raw JSON for debugging
        if raw_positions:
            log.info("T212 RAW (pos 1 of %d): %s",
                     len(raw_positions),
                     json.dumps(raw_positions[0], default=str, indent=2))

        # Normalise all positions
        positions = [_normalise_position(p) for p in raw_positions]

        for p in positions[:10]:
            log.info("  %-15s %-40s qty=%.4f avg=£%.2f price=£%.2f P/L=£%.2f value=£%.2f",
                     p["ticker"], p["name"][:40], p["quantity"],
                     p["averagePrice"], p["currentPrice"],
                     p["ppl"], p["value"])

        total_value = sum(p["value"] for p in positions)
        log.info("  TOTAL portfolio value: £%.2f (%d positions)", total_value, len(positions))

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

    T212 uses: VUAG_EQ_L, AAPL_US_EQ, CSPX_EQ_L, BRK.B_US_EQ, etc.
    yfinance uses: VUAG.L, AAPL, CSPX.L, BRK-B, etc.

    Exchange suffixes:
      _L  → .L (London Stock Exchange)
      _US → (no suffix, US default)
      _DE → .DE (Frankfurt)
      _AS → .AS (Amsterdam)
      _PA → .PA (Paris)
      _MI → .MI (Milan)
    """
    if not t212_ticker or t212_ticker == "UNKNOWN":
        return t212_ticker

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
        "LSE": ".L",
    }

    parts = t212_ticker.split("_")
    symbol = parts[0]

    # yfinance uses dashes instead of dots in symbols like BRK-B
    # T212 might use BRK.B_US_EQ
    # Actually yfinance uses BRK-B for Berkshire B shares
    yf_symbol = symbol.replace(".", "-") if "." in symbol else symbol

    # Find exchange code in the parts (skip "EQ" which is just equity type)
    for part in parts[1:]:
        if part == "EQ":
            continue
        if part in exchange_map:
            return f"{yf_symbol}{exchange_map[part]}"

    # Default: just the base symbol
    return yf_symbol
