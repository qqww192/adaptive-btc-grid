"""
conftest.py
Pre-mock Google libraries that depend on the cryptography C extension,
which may not be available in all environments (e.g. CI sandboxes).
"""

import sys
from unittest.mock import MagicMock

# These modules depend on cryptography's Rust/C backend.
# Mock them before any test file imports src modules.
MOCK_MODULES = [
    "google.generativeai",
    "google.oauth2",
    "google.oauth2.service_account",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.errors",
]

for mod in MOCK_MODULES:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
