"""
connection_test.py
Comprehensive smoke test — verifies all API connections and simulates both pipelines.

Test Case 1 (Daily):  Gemini news scan → Telegram alert
Test Case 2 (Weekly): T212 fetch → Gemini analysis → Google Drive report → Telegram snapshot
"""

import base64
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


# ── Individual service tests ─────────────────────────────────────────────────

def test_t212_connection() -> bool:
    """Test Trading 212 API connectivity and fetch positions."""
    api_key = os.environ.get("T212_API_KEY", "")
    secret_key = os.environ.get("T212_SECRET_KEY", "")
    if not api_key or not secret_key:
        log.error("  FAIL — T212_API_KEY or T212_SECRET_KEY not set.")
        return False

    credentials = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": f"Basic {credentials}"}
    try:
        resp = httpx.get(f"{T212_BASE}/equity/portfolio", headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        positions = resp.json()
        log.info(f"  PASS — Trading 212 connected. {len(positions)} positions found.")
        return True
    except httpx.HTTPStatusError as e:
        log.error(f"  FAIL — Trading 212 returned {e.response.status_code}: {e.response.text}")
        return False
    except httpx.RequestError as e:
        log.error(f"  FAIL — Network error: {e}")
        return False


def test_gemini_connection() -> bool:
    """Test Gemini API connectivity with a simple prompt."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("  FAIL — GEMINI_API_KEY not set.")
        return False

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Say 'connection OK' in one word.",
        )
        log.info(f"  PASS — Gemini connected. Response: {response.text.strip()}")
        return True
    except Exception as e:
        log.error(f"  FAIL — Gemini error: {e}")
        return False


def test_gemini_news_scan() -> bool:
    """Test Gemini can generate a news scan (used in daily alert pipeline)."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("  FAIL — GEMINI_API_KEY not set.")
        return False

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "You are a financial news scanner. Return a single-line test response: "
            "'News scan OK — no alerts today.'"
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
        )
        text = response.text.strip()
        log.info(f"  PASS — Gemini news scan working. Response: {text[:80]}")
        return True
    except Exception as e:
        log.error(f"  FAIL — Gemini news scan error: {e}")
        return False


def test_gemini_portfolio_analysis() -> bool:
    """Test Gemini can analyse a sample portfolio (used in weekly report pipeline)."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        log.error("  FAIL — GEMINI_API_KEY not set.")
        return False

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        prompt = (
            "Analyse this sample portfolio in one sentence:\n"
            "AAPL: 10 shares at $150, MSFT: 5 shares at $300.\n"
            "Just confirm you can analyse it by saying 'Analysis OK'."
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
        )
        text = response.text.strip()
        log.info(f"  PASS — Gemini portfolio analysis working. Response: {text[:80]}")
        return True
    except Exception as e:
        log.error(f"  FAIL — Gemini portfolio analysis error: {e}")
        return False


def test_google_drive_connection() -> bool:
    """Test Google Drive/Docs/Sheets API connectivity including write permissions."""
    sa_json = os.environ.get("GOOGLE_SA_JSON", "")
    if not sa_json:
        log.error("  FAIL — GOOGLE_SA_JSON not set.")
        return False

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        sa_dict = json.loads(sa_json)
        scopes = [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/spreadsheets",
        ]
        creds = service_account.Credentials.from_service_account_info(sa_dict, scopes=scopes)

        # --- Step A: Drive API — list files (read test) ---
        drive_service = build("drive", "v3", credentials=creds)
        results = drive_service.files().list(pageSize=1, fields="files(id, name)").execute()
        files = results.get("files", [])
        log.info(f"  PASS — Drive API read: {len(files)} file(s) visible to service account.")

        # --- Step B: Drive API — create and delete a temp file (write test) ---
        test_file = None
        try:
            test_file = drive_service.files().create(
                body={"name": "__connection_test_temp__", "mimeType": "application/vnd.google-apps.document"},
                fields="id",
            ).execute()
            log.info(f"  PASS — Drive API write: created test doc (id={test_file['id']}).")
        except HttpError as e:
            log.error(f"  FAIL — Drive API write: cannot create files: {e}")
            log.error("         Check: Google Drive API enabled? Service account has Editor/Owner role?")
            return False
        finally:
            # Always clean up the temp file
            if test_file:
                try:
                    drive_service.files().delete(fileId=test_file["id"]).execute()
                    log.info("  OK   — Cleaned up test doc.")
                except HttpError:
                    log.warning(f"  WARN — Could not delete test doc {test_file['id']}; delete it manually.")

        # --- Step C: Docs API — verify the API is reachable ---
        try:
            docs_service = build("docs", "v1", credentials=creds)
            # If GOOGLE_DOC_ID is set, verify access to the master doc
            doc_id = os.environ.get("GOOGLE_DOC_ID", "")
            if doc_id:
                doc = docs_service.documents().get(documentId=doc_id).execute()
                log.info(f"  PASS — Docs API: master doc accessible: '{doc.get('title', 'Untitled')}'")
            else:
                # Create a throwaway doc via Docs API to confirm it works, then delete via Drive
                temp_doc = docs_service.documents().create(body={"title": "__docs_api_test__"}).execute()
                temp_doc_id = temp_doc["documentId"]
                log.info(f"  PASS — Docs API write: created test doc (id={temp_doc_id}).")
                try:
                    drive_service.files().delete(fileId=temp_doc_id).execute()
                    log.info("  OK   — Cleaned up Docs API test doc.")
                except HttpError:
                    log.warning(f"  WARN — Could not delete Docs test doc {temp_doc_id}; delete it manually.")
        except HttpError as e:
            log.error(f"  FAIL — Docs API error: {e}")
            log.error("         Check: Google Docs API enabled in Cloud Console?")
            return False

        # --- Step D: Sheets API — verify the API is reachable ---
        try:
            sheets_service = build("sheets", "v4", credentials=creds)
            temp_sheet = sheets_service.spreadsheets().create(
                body={"properties": {"title": "__sheets_api_test__"}},
                fields="spreadsheetId",
            ).execute()
            temp_sheet_id = temp_sheet["spreadsheetId"]
            log.info(f"  PASS — Sheets API write: created test sheet (id={temp_sheet_id}).")
            try:
                drive_service.files().delete(fileId=temp_sheet_id).execute()
                log.info("  OK   — Cleaned up Sheets API test sheet.")
            except HttpError:
                log.warning(f"  WARN — Could not delete test sheet {temp_sheet_id}; delete it manually.")
        except HttpError as e:
            log.error(f"  FAIL — Sheets API error: {e}")
            log.error("         Check: Google Sheets API enabled in Cloud Console?")
            return False

        log.info("  PASS — All Google API write permissions verified.")
        return True
    except Exception as e:
        log.error(f"  FAIL — Google API error: {e}")
        return False


def test_telegram_connection() -> bool:
    """Test Telegram Bot API connectivity (does NOT send a message)."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token:
        log.error("  FAIL — TELEGRAM_BOT_TOKEN not set.")
        return False
    if not chat_id:
        log.error("  FAIL — TELEGRAM_CHAT_ID not set.")
        return False

    try:
        # getMe just verifies the bot token is valid — sends nothing
        resp = httpx.get(
            f"https://api.telegram.org/bot{bot_token}/getMe",
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            bot_name = data["result"].get("username", "unknown")
            log.info(f"  PASS — Telegram bot connected: @{bot_name}")
            return True
        else:
            log.error(f"  FAIL — Telegram API returned: {data}")
            return False
    except httpx.HTTPStatusError as e:
        log.error(f"  FAIL — Telegram returned {e.response.status_code}: {e.response.text}")
        return False
    except httpx.RequestError as e:
        log.error(f"  FAIL — Network error: {e}")
        return False


def test_telegram_send() -> bool:
    """Send a test message to Telegram to verify chat_id works."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        log.error("  FAIL — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        return False

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "T212 Portfolio Checker — connection test passed!",
                "parse_mode": "Markdown",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            log.info("  PASS — Telegram test message sent successfully.")
            return True
        else:
            log.error(f"  FAIL — Telegram send returned: {data}")
            return False
    except Exception as e:
        log.error(f"  FAIL — Telegram send error: {e}")
        return False


# ── Test cases (pipelines) ───────────────────────────────────────────────────

def run_test_case_1() -> dict[str, bool]:
    """
    Test Case 1 — Daily News Alert Pipeline
    Steps: Gemini news scan → Telegram alert
    """
    results = {}

    log.info("  Step 1/2: Gemini news scan...")
    results["1.1 Gemini news scan"] = test_gemini_news_scan()

    log.info("  Step 2/2: Telegram bot connection...")
    results["1.2 Telegram bot"] = test_telegram_connection()

    log.info("  Step 2/2: Telegram send test message...")
    results["1.3 Telegram send"] = test_telegram_send()

    return results


def run_test_case_2() -> dict[str, bool]:
    """
    Test Case 2 — Weekly Portfolio Report Pipeline
    Steps: T212 fetch → Gemini analysis → Google Drive report → Telegram snapshot
    """
    results = {}

    log.info("  Step 1/4: Trading 212 portfolio fetch...")
    results["2.1 Trading 212 fetch"] = test_t212_connection()

    log.info("  Step 2/4: Gemini portfolio analysis...")
    results["2.2 Gemini analysis"] = test_gemini_portfolio_analysis()

    log.info("  Step 3/4: Google Drive / Docs connection...")
    results["2.3 Google Drive"] = test_google_drive_connection()

    log.info("  Step 4/4: Telegram snapshot delivery...")
    results["2.4 Telegram delivery"] = test_telegram_connection()

    return results


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    load_dotenv()

    all_results = {}

    # ── Test Case 1 ──────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("TEST CASE 1: Daily News Alert Pipeline")
    log.info("  Flow: Gemini news scan -> Telegram alert")
    log.info("=" * 60)
    tc1 = run_test_case_1()
    all_results.update(tc1)

    # ── Test Case 2 ──────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("TEST CASE 2: Weekly Portfolio Report Pipeline")
    log.info("  Flow: T212 fetch -> Gemini analysis -> Google Drive -> Telegram")
    log.info("=" * 60)
    tc2 = run_test_case_2()
    all_results.update(tc2)

    # ── Summary ──────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("RESULTS SUMMARY")
    log.info("=" * 60)

    passed = 0
    failed = 0
    for name, ok in all_results.items():
        status = "PASS" if ok else "FAIL"
        icon = "+" if ok else "X"
        log.info(f"  [{icon}] {name}: {status}")
        if ok:
            passed += 1
        else:
            failed += 1

    log.info("-" * 60)
    log.info(f"  Total: {passed + failed} | Passed: {passed} | Failed: {failed}")
    log.info("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
