"""
Tests for analyse.py
"""

import os
import sys
import importlib
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Import after conftest has mocked google modules
import analyse
from analyse import _build_prompt, analyse_portfolio


SAMPLE_POSITIONS = [
    {"ticker": "AAPL", "quantity": 10, "averagePrice": 140.0, "currentPrice": 150.0, "ppl": 100.0},
    {"ticker": "MSFT", "quantity": 5, "averagePrice": 280.0, "currentPrice": 300.0, "ppl": 100.0},
]


class TestBuildPrompt:
    def test_contains_tickers(self):
        prompt = _build_prompt(SAMPLE_POSITIONS, "2026-03-24 07:00 UTC")
        assert "AAPL" in prompt
        assert "MSFT" in prompt

    def test_contains_date(self):
        prompt = _build_prompt(SAMPLE_POSITIONS, "2026-03-24 07:00 UTC")
        assert "2026-03-24" in prompt

    def test_contains_instructions(self):
        prompt = _build_prompt(SAMPLE_POSITIONS, "2026-03-24 07:00 UTC")
        assert "risk rating" in prompt.lower()
        assert "portfolio summary" in prompt.lower()


class TestAnalysePortfolio:
    @patch("analyse.genai")
    def test_analyse_returns_text(self, mock_genai):
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "# Portfolio Report\nAll looks good."
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            result = analyse_portfolio(SAMPLE_POSITIONS, "2026-03-24 07:00 UTC")

        assert "Portfolio Report" in result
        mock_genai.configure.assert_called_once_with(api_key="test-key")

    def test_missing_api_key_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="GEMINI_API_KEY"):
                analyse_portfolio(SAMPLE_POSITIONS, "2026-03-24 07:00 UTC")
