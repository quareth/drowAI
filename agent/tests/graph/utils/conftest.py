"""Pytest configuration for graph utils tests.

This conftest sets up import paths to allow testing the llm_resolver
without triggering heavy imports from the full graph module.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Get the root of the project
PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Add project root to path for absolute imports
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Add providers path for llm imports
LLM_PROVIDER_PATH = PROJECT_ROOT / "agent" / "providers"
if str(LLM_PROVIDER_PATH) not in sys.path:
    sys.path.insert(0, str(LLM_PROVIDER_PATH))

