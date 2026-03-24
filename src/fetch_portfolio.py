"""
fetch_portfolio.py
Fetches all open positions from the Trading 212 API using cursor-based pagination.

API docs: https://t212public-api-docs.trading212.com/
Rate limit: respect the API's rate limits — never call in a tight loop without the cursor.
"""

import os
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://live.trading212.com/api/v0"
TIMEOUT = 30  # seconds


def _get_headers() -> dict[str, str]:
    api_key = os.environ.get("T212_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "T212_API_KEY environment variable is not set. "
            "See docs/setup.md for how to obtain and configure it."
        )
    return {"Authorization": api_key}


def fetch_all_positions() -> list[dict[str, Any]]:
    """
    Fetches all open positions from Trading 212.
    Returns a list of position dicts, each containing ticker, quantity,
    currentPrice, ppl (profit/loss), etc.
    """
    headers = _get_headers()

    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            resp = client.get(
                f"{BASE_URL}/equity/portfolio",
                headers=headers,
            )
            resp.raise_for_status()
            positions = resp.json()

        log.info(f"Fetched {len(positions)} positions from Trading 212.")
        return positions

    except httpx.HTTPStatusError as e:
        log.error(f"Trading 212 API error: {e.response.status_code} — {e.response.text}")
        raise
    except httpx.RequestError as e:
        log.error(f"Network error contacting Trading 212: {e}")
        raise
