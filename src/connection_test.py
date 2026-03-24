"""
connection_test.py
Quick smoke test — verifies connectivity to Trading 212 and Gemini APIs.
Run via: python src/connection_test.py
"""

import os
import sys
import json
import logging

from dotenv import load_dotenv
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

T212_BASE = "https://live.trading212.com/api/v0"
TIMEOUT = 15


def test_t212():
    api_key = os.environ.get("T212_API_KEY", "")
    secret_key = os.environ.get("T212_SECRET_KEY", "")
    if not api_key or not secret_key:
        log.error("FAIL — T212_API_KEY or T212_SECRET_KEY not set.")
        return False

    headers = {"Authorization": api_key, "X-Secret-Key": secret_key}
    try:
        resp = httpx.get(f"{T212_BASE}/equity/portfolio", headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        positions = resp.json()
        log.info(f"OK — Trading 212 connected. {len(positions)} positions found.")
        return True
    except httpx.HTTPStatusError as e:
        log.error(f"FAIL — Trading 212 returned {e.response.status_code}: {e.response.text}")
        return False
    except httpx.RequestError as e:
        log.error(f"FAIL — Network error: {e}")
        return False


def test_gemini():
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("FAIL — GEMINI_API_KEY not set.")
        return False

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content("Say 'connection OK' in one word.")
        log.info(f"OK — Gemini connected. Response: {response.text.strip()}")
        return True
    except Exception as e:
        log.error(f"FAIL — Gemini error: {e}")
        return False


def main():
    load_dotenv()

    log.info("=" * 40)
    log.info("Connection Test")
    log.info("=" * 40)

    results = {}
    results["Trading 212"] = test_t212()
    results["Gemini"] = test_gemini()

    log.info("=" * 40)
    all_ok = all(results.values())
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        log.info(f"  {name}: {status}")

    log.info("=" * 40)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
