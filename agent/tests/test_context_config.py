"""Tests for context configuration defaults and validation rules."""

import os
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.context.config import ContextConfig


def test_env_defaults(monkeypatch):
    monkeypatch.delenv("CONTEXT_TOKEN_LIMIT", raising=False)
    cfg = ContextConfig()
    assert cfg.target_token_limit == 3000
    assert cfg.enable_compression is True


def test_custom_config_overrides():
    cfg = ContextConfig({"target_token_limit": 2000, "breakdown": {"tool_results": 300}})
    assert cfg.target_token_limit == 2000
    assert cfg.breakdown["tool_results"] == 300


def test_provider_adjustment():
    cfg = ContextConfig()
    cfg.adjust_for_provider("anthropic")
    assert cfg.target_token_limit == 3500
    assert cfg.breakdown["artifacts"] >= 800
    cfg.adjust_for_provider("gemini")
    assert cfg.target_token_limit == 2800
    assert cfg.compression_level == "aggressive"


def test_validation():
    cfg = ContextConfig({"target_token_limit": -1})
    assert cfg.validate() is False
    cfg = ContextConfig({"cleanup_interval": -5})
    assert cfg.validate() is False
    cfg = ContextConfig({"compression_level": "bad"})
    assert cfg.validate() is False
