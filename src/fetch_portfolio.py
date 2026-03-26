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


def fetch_all_positions() -> list[dict[str, Any]]:
    """
    Fetches all open positions from Trading 212.

    Returns a list of position dicts:
      {ticker, quantity, averagePrice, currentPrice, ppl, fxPpl,
       pieQuantity, initialFillDate, frontend, maxBuy, maxSell}
    """
    headers = _get_headers()

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{BASE_URL}/equity/positions",
                headers=headers,
            )
            resp.raise_for_status()
            positions = resp.json()

        log.info("Fetched %d positions from Trading 212.", len(positions))

        # Log first few for debugging
        for p in positions[:3]:
            log.info("  T212 raw: ticker=%s qty=%s avg=%s price=%s ppl=%s pie_qty=%s",
                     p.get("ticker"), p.get("quantity"), p.get("averagePrice"),
                     p.get("currentPrice"), p.get("ppl"), p.get("pieQuantity"))

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
        for pie in pies:
            log.info("  Pie: id=%s", pie.get("id"))
        return pies

    except httpx.HTTPStatusError as e:
        log.error("Trading 212 pies API error: %s — %s", e.response.status_code, e.response.text)
        return []
    except httpx.RequestError as e:
        log.error("Network error fetching pies: %s", e)
        return []
