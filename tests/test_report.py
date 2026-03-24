"""
Tests for report.py — Google Drive integration.
All Google API calls are mocked (via conftest.py).
"""

import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Import after conftest has mocked google modules
import report
from report import _get_credentials, write_report_to_drive


class TestGetCredentials:
    def test_missing_sa_json_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="GOOGLE_SA_JSON"):
                _get_credentials()

    def test_valid_sa_json(self):
        sa_json = json.dumps({"type": "service_account", "project_id": "test"})
        mock_creds = MagicMock()
        with patch.dict(os.environ, {"GOOGLE_SA_JSON": sa_json}):
            with patch("report.service_account.Credentials.from_service_account_info", return_value=mock_creds):
                creds = _get_credentials()
                assert creds == mock_creds


class TestWriteReportToDrive:
    def test_returns_doc_url(self):
        sa_json = json.dumps({"type": "service_account", "project_id": "test"})

        mock_docs = MagicMock()
        mock_doc_get = {"body": {"content": [{"endIndex": 2}]}}
        mock_docs.documents().get().execute.return_value = mock_doc_get

        mock_drive = MagicMock()

        def build_side_effect(service, version, credentials=None):
            if service == "docs":
                return mock_docs
            return mock_drive

        with patch.dict(os.environ, {"GOOGLE_SA_JSON": sa_json, "GOOGLE_DOC_ID": "doc123"}):
            with patch("report._get_credentials", return_value=MagicMock()):
                with patch("report.build", side_effect=build_side_effect):
                    # Need to reload to pick up the env var for MASTER_DOC_ID
                    with patch("report.MASTER_DOC_ID", "doc123"):
                        url = write_report_to_drive("# Test Report", "2026-03-24")
                        assert "doc123" in url
                        assert "docs.google.com" in url
