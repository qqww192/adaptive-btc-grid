"""
report.py
Writes the analysis report to Google Drive as a Google Doc.

Authentication: uses a Google Service Account with the Drive API scope.
The service account JSON is stored as a GitHub Secret (GOOGLE_SA_JSON)
and written to a temp file at runtime — never committed to the repo.

Each run appends a new dated section to a persistent master document.
If no master doc exists yet, one is created automatically.
"""

import os
import json
import logging
import tempfile
from datetime import datetime
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

# The ID of your master Google Doc — set this after first run (see docs/setup.md)
# If empty, a new doc is created and its ID is printed to logs for you to save.
MASTER_DOC_ID: str = os.environ.get("GOOGLE_DOC_ID", "")
REPORT_FOLDER_ID: str = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")  # optional: file goes here


def _get_credentials() -> service_account.Credentials:
    """
    Loads service account credentials from the GOOGLE_SA_JSON env var.
    In GitHub Actions this is injected from Secrets; locally from .env.
    """
    sa_json = os.environ.get("GOOGLE_SA_JSON", "")
    if not sa_json:
        raise EnvironmentError(
            "GOOGLE_SA_JSON environment variable is not set. "
            "See docs/setup.md for how to create and configure the service account."
        )

    # Write to a temp file — google-auth needs a file path or dict
    sa_dict = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(sa_dict, scopes=SCOPES)
    return creds


def _get_or_create_doc(docs_service, drive_service, run_date: str) -> str:
    """
    Returns the master doc ID. Creates the doc if MASTER_DOC_ID is not set.
    """
    if MASTER_DOC_ID:
        return MASTER_DOC_ID

    log.info("GOOGLE_DOC_ID not set — creating a new master document.")
    body = {"title": "T212 Portfolio Reports"}

    # Create the doc
    doc = docs_service.documents().create(body=body).execute()
    doc_id: str = doc["documentId"]

    # Move to folder if specified
    if REPORT_FOLDER_ID:
        drive_service.files().update(
            fileId=doc_id,
            addParents=REPORT_FOLDER_ID,
            removeParents="root",
            fields="id, parents",
        ).execute()

    log.info(
        f"\n{'='*60}\n"
        f"New Google Doc created!\n"
        f"Doc ID: {doc_id}\n"
        f"Set this as GOOGLE_DOC_ID in your GitHub Secrets so future\n"
        f"runs append to the same document.\n"
        f"{'='*60}"
    )
    return doc_id


def _append_to_doc(docs_service, doc_id: str, report_markdown: str, run_date: str) -> None:
    """
    Appends a horizontal divider + the new report to the end of the document.
    Google Docs API uses index-based insertions; we always insert at the end.
    """
    # Get current doc to find end index
    doc = docs_service.documents().get(documentId=doc_id).execute()
    body_content = doc.get("body", {}).get("content", [])
    end_index = body_content[-1].get("endIndex", 1) - 1  # -1 to stay before the final newline

    header = f"\n\n{'─' * 60}\n\n"
    full_text = header + report_markdown + "\n\n"

    requests = [
        {
            "insertText": {
                "location": {"index": end_index},
                "text": full_text,
            }
        }
    ]

    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()


def write_alert_log_to_drive(alert_markdown: str, scan_date: str) -> str:
    """
    Appends a daily news scan log to the master Google Doc.
    Uses the same doc as the weekly portfolio report (separated by dividers).
    Returns the document URL.
    """
    creds = _get_credentials()
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    doc_id = _get_or_create_doc(docs_service, drive_service, scan_date)

    try:
        _append_to_doc(docs_service, doc_id, alert_markdown, scan_date)
        log.info(f"Alert log appended to doc {doc_id}")
    except HttpError as e:
        log.error(f"Google Docs API error writing alert log: {e}")
        raise

    return f"https://docs.google.com/document/d/{doc_id}/edit"


def write_report_to_drive(report_markdown: str, run_date: str) -> str:
    """
    Main entry point. Appends the report to the master Google Doc.
    Returns the URL of the document.
    """
    creds = _get_credentials()
    docs_service = build("docs", "v1", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)

    doc_id = _get_or_create_doc(docs_service, drive_service, run_date)

    try:
        _append_to_doc(docs_service, doc_id, report_markdown, run_date)
        log.info(f"Report appended to doc {doc_id}")
    except HttpError as e:
        log.error(f"Google Docs API error: {e}")
        raise

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    return doc_url
