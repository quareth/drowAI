"""Tests for token budget fitting and provider defaults."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from agent.context.token_manager import TokenManager
except Exception:
    from context.token_manager import TokenManager


def sample_context():
    return {
        "system_context": {"a": "b"},
        "recent_cycles": [{"i": i} for i in range(5)],
        "tool_results": [{"summary": "x", "importance_score": 1.0} for _ in range(6)],
        "artifacts": [{"text": "x" * 100} for _ in range(10)],
    }


def test_fit_to_budget_truncates():
    mgr = TokenManager(provider="openai")
    context, tokens = mgr.fit_to_budget(sample_context())
    assert tokens <= mgr.target
    assert context["system_context"] == {"a": "b"}
    assert len(context["recent_cycles"]) <= 5


def test_provider_defaults():
    anth = TokenManager(provider="anthropic")
    gem = TokenManager(provider="gemini")
    assert anth.target == 3500
    assert gem.target == 2800


def test_approx_tokens_accuracy():
    mgr = TokenManager()
    text = "x" * 100
    accurate_count = mgr._approx_tokens(text)
    old_approx = len(text) // 4
    
    # The new accurate counting should be different from the old approximation
    # but both should be reasonable values
    assert accurate_count > 0
    assert accurate_count <= len(text)  # Shouldn't be more than character count
    
    # The accurate count should be different from the old approximation
    # (this is the whole point of the improvement)
    assert accurate_count != old_approx
