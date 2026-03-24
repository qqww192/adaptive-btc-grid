"""
fetch_portfolio.py
Fetches all open positions from Trading 212 using their paginated REST API.
Docs: https://docs.trading212.com/api/section/pagination/example
"""

import os
import logging
import httpx
from typing import Any

log = logging.getLogger(__name__)

T212_BASE_URL = "https://live.trading212.com/api/v0"  # swap for paper: trading212.com/api/v0
T212_API_KEY = os.environ["T212_API_KEY"]

HEADERS = {
    "Authorization": T212_API_KEY,
    "Content-Type": "application/json",
}

# Trading 212 max page size
PAGE_LIMIT = 50


def fetch_all_positions() -> list[dict[str, Any]]:
    """
    Retrieves every open position using cursor-based pagination.
    Returns a flat list of position dicts.
    """
    positions: list[dict[str, Any]] = []
    cursor: str | None = None

    with httpx.Client(headers=HEADERS, timeout=30) as client:
        while True:
            params: dict[str, Any] = {"limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor

            log.debug(f"  Fetching page — cursor={cursor}")
            response = client.get(f"{T212_BASE_URL}/equity/portfolio", params=params)
            response.raise_for_status()

            data = response.json()

            # T212 returns {"items": [...], "nextCursor": "..." | null}
            items: list[dict] = data.get("items", [])
            positions.extend(items)

            cursor = data.get("nextCursor")
            if not cursor:
                break  # no more pages

    log.info(f"Fetched {len(positions)} positions across all pages.")
    return positions


def fetch_account_summary() -> dict[str, Any]:
    """
    Fetches account-level cash and P&L summary.
    """
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        response = client.get(f"{T212_BASE_URL}/equity/account/summary")
        response.raise_for_status()
        return response.json()


def fetch_instrument_details(ticker: str) -> dict[str, Any]:
    """
    Fetches metadata for a single instrument (sector, ISIN, exchange, etc.)
    Useful for enriching positions before analysis.
    """
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        response = client.get(
            f"{T212_BASE_URL}/equity/metadata/instruments",
            params={"ticker": ticker},
        )
        response.raise_for_status()
        data = response.json()
        # API returns a list; grab first match
        return data[0] if data else {}
