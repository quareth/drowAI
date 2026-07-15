"""Pytest configuration for canonical LLM provider package tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Get the root of the project
PROJECT_ROOT = Path(__file__).resolve().parents[4]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
