"""
Tests for fetch_portfolio.py
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Add src to path so we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fetch_portfolio import _get_headers, fetch_all_positions


class TestGetHeaders:
    def test_missing_api_key_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="T212_API_KEY"):
                _get_headers()

    def test_missing_secret_key_raises(self):
        with patch.dict(os.environ, {"T212_API_KEY": "key123"}, clear=True):
            with pytest.raises(EnvironmentError, match="T212_SECRET_KEY"):
                _get_headers()

    def test_both_keys_present(self):
        with patch.dict(os.environ, {"T212_API_KEY": "key123", "T212_SECRET_KEY": "secret456"}, clear=True):
            headers = _get_headers()
            assert headers["Authorization"] == "key123"
            assert headers["X-Secret-Key"] == "secret456"


class TestFetchAllPositions:
    @patch("fetch_portfolio._get_headers", return_value={"Authorization": "k", "X-Secret-Key": "s"})
    @patch("fetch_portfolio.httpx.Client")
    def test_successful_fetch(self, mock_client_cls, mock_headers):
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"ticker": "AAPL", "quantity": 10, "currentPrice": 150.0, "ppl": 50.0},
            {"ticker": "MSFT", "quantity": 5, "currentPrice": 300.0, "ppl": 25.0},
        ]
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        positions = fetch_all_positions()
        assert len(positions) == 2
        assert positions[0]["ticker"] == "AAPL"
        assert positions[1]["ticker"] == "MSFT"

    @patch("fetch_portfolio._get_headers", return_value={"Authorization": "k", "X-Secret-Key": "s"})
    @patch("fetch_portfolio.httpx.Client")
    def test_empty_portfolio(self, mock_client_cls, mock_headers):
        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        positions = fetch_all_positions()
        assert positions == []
