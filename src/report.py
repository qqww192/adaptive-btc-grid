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


def _find_existing_report_doc(drive_service) -> Optional[str]:
    """
    Search for an existing 'T212 Portfolio Reports' Google Doc owned by this
    service account. Returns the doc ID if found, None otherwise.
    """
    try:
        results = drive_service.files().list(
            q="name = 'T212 Portfolio Reports' and mimeType = 'application/vnd.google-apps.document' and trashed = false",
            fields="files(id)",
            pageSize=1,
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
    except HttpError as e:
        log.warning("Failed to search for existing report doc: %s", e)
    return None


def _empty_trash(drive_service) -> None:
    """Attempt to empty the service account's Drive trash to reclaim quota."""
    try:
        drive_service.files().emptyTrash().execute()
        log.info("Emptied Drive trash to reclaim storage quota.")
    except HttpError as e:
        log.warning("Failed to empty Drive trash: %s", e)


def _reclaim_storage(drive_service, keep_ids: set[str] | None = None) -> None:
    """Delete all SA-owned files except those in keep_ids, then empty trash."""
    keep_ids = keep_ids or set()
    page_token = None
    deleted = 0
    while True:
        resp = drive_service.files().list(
            pageSize=100,
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        for f in resp.get("files", []):
            if f["id"] not in keep_ids:
                try:
                    drive_service.files().delete(fileId=f["id"]).execute()
                    deleted += 1
                except HttpError:
                    pass
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if deleted:
        log.info("Deleted %d old file(s) to free storage.", deleted)
    _empty_trash(drive_service)


def _create_doc(drive_service, file_metadata: dict) -> dict:
    """Create a doc, retrying once after reclaiming storage if quota is exceeded."""
    try:
        return drive_service.files().create(
            body=file_metadata,
            fields="id",
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 403 and "storageQuotaExceeded" in str(exc):
            log.warning("Storage quota exceeded — deleting old files and retrying.")
            # Keep the master doc if it exists
            keep = {MASTER_DOC_ID} if MASTER_DOC_ID else set()
            _reclaim_storage(drive_service, keep_ids=keep)
            return drive_service.files().create(
                body=file_metadata,
                fields="id",
            ).execute()
        raise


def _get_or_create_doc(docs_service, drive_service, run_date: str) -> str:
    """
    Returns the master doc ID. Creates the doc if MASTER_DOC_ID is not set.
    """
    if MASTER_DOC_ID:
        # Validate the doc still exists / is accessible before using it
        try:
            docs_service.documents().get(documentId=MASTER_DOC_ID).execute()
            return MASTER_DOC_ID
        except HttpError as e:
            log.warning(
                "GOOGLE_DOC_ID '%s' is not accessible (%s). "
                "Will try to find or create a document instead.",
                MASTER_DOC_ID, e,
            )

    # Before creating a new doc, check if one already exists from a previous run
    existing_id = _find_existing_report_doc(drive_service)
    if existing_id:
        log.info("Found existing report doc %s — reusing it.", existing_id)
        return existing_id

    log.info("No existing report doc found — creating a new master document.")

    # Create the doc via Drive API
    file_metadata = {
        "name": "T212 Portfolio Reports",
        "mimeType": "application/vnd.google-apps.document",
    }
    if REPORT_FOLDER_ID:
        # Create directly in the user's shared folder — uses folder owner's quota
        file_metadata["parents"] = [REPORT_FOLDER_ID]
        log.info("Creating doc in shared folder %s (uses folder owner's quota).", REPORT_FOLDER_ID)
    else:
        log.warning(
            "GOOGLE_DRIVE_FOLDER_ID not set. Creating in service account's Drive. "
            "This may fail if the SA has no storage quota. "
            "Set GOOGLE_DRIVE_FOLDER_ID to a folder shared with the SA."
        )

    file = _create_doc(drive_service, file_metadata)
    doc_id: str = file["id"]

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
