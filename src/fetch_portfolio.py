"""
fetch_portfolio.py
Fetches portfolio data from the Trading 212 API via the Pies endpoint.

API docs: https://docs.trading212.com/api/pies-(deprecated)/getall
Endpoints:
  - GET /equity/pies         — list all pies (id, cash, progress)
  - GET /equity/pies/{id}    — pie detail with instruments

Each instrument in a pie has:
  ticker, name, ownedQuantity, currentShare, expectedShare,
  result: {investedValue, result, resultCoef, value}

Auth: Basic Auth (API_KEY:SECRET_KEY base64-encoded) or legacy API key header.
"""

import base64
import json
import os
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://live.trading212.com/api/v0"
TIMEOUT = 30  # seconds

# Currencies where price is in minor units (pence)
MINOR_CURRENCY_CODES = {"GBX", "GBp", "ILA"}


def _get_headers() -> dict[str, str]:
    """Build auth headers for T212 API (Basic Auth)."""
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
        # Fallback: pass API key directly
        return {"Authorization": api_key}


def _get(client: httpx.Client, path: str) -> Any:
    """GET request with auth headers and error handling."""
    headers = _get_headers()
    resp = client.get(f"{BASE_URL}{path}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def fetch_all_pies() -> list[dict[str, Any]]:
    """
    Fetches all pies (summary) from Trading 212.
    Returns list of: {id, cash, dividendDetails, progress, status}
    """
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            pies = _get(client, "/equity/pies")

        if not isinstance(pies, list):
            log.warning("Unexpected pies response type: %s", type(pies))
            pies = pies.get("items", []) if isinstance(pies, dict) else []

        log.info("Fetched %d pies from Trading 212.", len(pies))
        if pies:
            log.info("T212 RAW PIE SUMMARY (first): %s",
                     json.dumps(pies[0], default=str))
        return pies

    except httpx.HTTPStatusError as e:
        log.error("T212 pies API error: %s — %s", e.response.status_code, e.response.text)
        raise
    except httpx.RequestError as e:
        log.error("Network error fetching pies: %s", e)
        raise


def fetch_pie_detail(pie_id: int) -> dict[str, Any]:
    """
    Fetches detail for a single pie including all instruments.

    Returns: {settings: {name, ...}, instruments: [{ticker, name, ownedQuantity,
              currentShare, result: {value, investedValue, result, resultCoef}, ...}],
              cash, dividendDetails, progress}
    """
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                detail = _get(client, f"/equity/pies/{pie_id}")

            log.info("Fetched pie %d: %s (%d instruments)",
                     pie_id,
                     detail.get("settings", {}).get("name", "?"),
                     len(detail.get("instruments", [])))
            return detail

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < max_retries:
                wait = 5 * attempt
                log.warning("Rate-limited on pie %d (attempt %d/%d). Waiting %ds...",
                            pie_id, attempt, max_retries, wait)
                time.sleep(wait)
            else:
                log.error("T212 pie detail API error for %d: %s — %s",
                          pie_id, e.response.status_code, e.response.text)
                return {}
        except httpx.RequestError as e:
            log.error("Network error fetching pie %d: %s", pie_id, e)
            return {}
    return {}


def fetch_portfolio() -> list[dict[str, Any]]:
    """
    Fetches all positions across all pies.
    Calls GET /equity/pies then GET /equity/pies/{id} for each.

    Returns a flat list of normalised position dicts:
      {ticker, name, pieName, quantity, value, investedValue, ppl, currentShare, weight}
    """
    pies_summary = fetch_all_pies()
    if not pies_summary:
        return []

    all_positions = []

    for i, pie_summary in enumerate(pies_summary):
        pie_id = pie_summary.get("id")
        if not pie_id:
            continue

        # T212 API rate limit: wait between requests
        if i > 0:
            time.sleep(5)

        detail = fetch_pie_detail(pie_id)
        if not detail:
            continue

        pie_name = detail.get("settings", {}).get("name", f"Pie {pie_id}")
        instruments = detail.get("instruments", [])

        # Log raw first instrument for debugging
        if instruments:
            log.info("  RAW instrument (first in %s): %s",
                     pie_name, json.dumps(instruments[0], default=str))

        for inst in instruments:
            ticker = inst.get("ticker", "")
            if not ticker:
                continue

            result = inst.get("result") or {}
            # T212 uses priceAvg-prefixed keys in pie instrument results
            value = float(result.get("priceAvgValue", 0) or result.get("value", 0))
            invested = float(result.get("priceAvgInvestedValue", 0) or result.get("investedValue", 0))
            ppl = float(result.get("priceAvgResult", 0) or result.get("result", 0))

            qty = float(inst.get("ownedQuantity", 0))
            current_share = float(inst.get("currentShare", 0))
            currency_code = inst.get("currencyCode", "")

            # Compute current price from value/qty
            current_price = (value / qty) if qty > 0 else 0
            avg_price = (invested / qty) if qty > 0 else 0

            # Convert minor currencies (GBX pence → GBP)
            if currency_code.upper() in MINOR_CURRENCY_CODES:
                current_price = current_price / 100
                avg_price = avg_price / 100

            all_positions.append({
                "ticker": ticker,
                "name": inst.get("name", ""),
                "pieName": pie_name,
                "quantity": qty,
                "averagePrice": avg_price,
                "currentPrice": current_price,
                "value": value,
                "investedValue": invested,
                "ppl": ppl,
                "currentShare": current_share,
                "currencyCode": currency_code,
            })

    # Log summary
    total_value = sum(p["value"] for p in all_positions)
    log.info("Portfolio: %d positions across %d pies. Total value: £%.2f",
             len(all_positions), len(pies_summary), total_value)
    for p in all_positions:
        weight = (p["value"] / total_value * 100) if total_value else 0
        log.info("  [%s] %-15s %-35s qty=%.4f value=£%.2f (%.1f%%)",
                 p["pieName"][:10], p["ticker"], p["name"][:35],
                 p["quantity"], p["value"], weight)

    return all_positions


def t212_to_yfinance(t212_ticker: str) -> str:
    """
    Convert T212 ticker format to yfinance ticker format.

    T212 actual formats (from pies API):
      VUAGl_EQ      → VUAG.L     (lowercase 'l' suffix = London)
      BRK_B_US_EQ   → BRK-B      (US stock, dot→dash)
      AAPL_US_EQ    → AAPL       (US stock)
      VWRPl_EQ      → VWRP.L     (London)
      SSLNl_EQ      → SSLN.L     (London)
    """
    if not t212_ticker or t212_ticker == "UNKNOWN":
        return t212_ticker

    # Exchange suffix map (lowercase letter at end of symbol before _EQ)
    exchange_suffix_map = {
        "l": ".L",     # London
        "d": ".DE",    # Germany (Deutsche Börse)
        "a": ".AS",    # Amsterdam
        "p": ".PA",    # Paris
        "m": ".MI",    # Milan
    }

    parts = t212_ticker.split("_")

    # Check for US stocks: e.g. BRK_B_US_EQ or AAPL_US_EQ
    if "US" in parts:
        # Reconstruct symbol from parts before "US"
        us_idx = parts.index("US")
        symbol = "_".join(parts[:us_idx])
        # yfinance uses dashes not underscores: BRK_B → BRK-B
        return symbol.replace("_", "-")

    # Check for exchange suffix on the symbol: e.g. VUAGl_EQ
    symbol = parts[0]
    if len(symbol) > 1 and symbol[-1] in exchange_suffix_map:
        base = symbol[:-1]  # strip the exchange letter
        suffix = exchange_suffix_map[symbol[-1]]
        return f"{base}{suffix}"

    return symbol
