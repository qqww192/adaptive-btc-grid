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
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            detail = _get(client, f"/equity/pies/{pie_id}")

        log.info("Fetched pie %d: %s (%d instruments)",
                 pie_id,
                 detail.get("settings", {}).get("name", "?"),
                 len(detail.get("instruments", [])))
        return detail

    except httpx.HTTPStatusError as e:
        log.error("T212 pie detail API error for %d: %s — %s",
                  pie_id, e.response.status_code, e.response.text)
        return {}
    except httpx.RequestError as e:
        log.error("Network error fetching pie %d: %s", pie_id, e)
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

    for pie_summary in pies_summary:
        pie_id = pie_summary.get("id")
        if not pie_id:
            continue

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
            value = float(result.get("value", 0))
            invested = float(result.get("investedValue", 0))
            ppl = float(result.get("result", 0))

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

    T212 uses: VUAG_EQ_L, AAPL_US_EQ, CSPX_EQ_L, BRK.B_US_EQ, etc.
    yfinance uses: VUAG.L, AAPL, CSPX.L, BRK-B, etc.
    """
    if not t212_ticker or t212_ticker == "UNKNOWN":
        return t212_ticker

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

    # yfinance uses dashes not dots in symbols like BRK-B
    yf_symbol = symbol.replace(".", "-") if "." in symbol else symbol

    # Find exchange code (skip "EQ" which is just equity type)
    for part in parts[1:]:
        if part == "EQ":
            continue
        if part in exchange_map:
            return f"{yf_symbol}{exchange_map[part]}"

    return yf_symbol
