"""Performance benchmarks for prompt template loading and registry lookup.

Validates refactoring acceptance criteria:
- Template loading (cached): < 1 ms
- Registry lookup: < 1 ms (cached; 0.1 ms on fast hosts)
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.prompts.loader import TemplateLoader
from core.prompts.registry import PromptRegistry


def test_template_loading_cached_under_1ms() -> None:
    """Cached template load should be under 1 ms."""
    root = Path(__file__).resolve().parent.parent / "versions"
    if not root.exists():
        pytest.skip("core/prompts/versions not found")
    loader = TemplateLoader(root)
    loader.cache_clear()
    # Prime cache
    loader.load(Path("intent") / "v1" / "intent_classifier.txt")
    # Measure cached load
    start = time.perf_counter()
    for _ in range(100):
        loader.load(Path("intent") / "v1" / "intent_classifier.txt")
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_load_ms = elapsed_ms / 100
    assert per_load_ms < 1.0, f"Cached load took {per_load_ms:.3f} ms (expected < 1 ms)"


def test_registry_lookup_under_1ms() -> None:
    """Registry get_template (cached) should be under 1 ms."""
    root = Path(__file__).resolve().parent.parent / "versions"
    if not root.exists():
        pytest.skip("core/prompts/versions not found")
    registry = PromptRegistry(templates_root=root)
    # Prime cache (template_id style)
    registry.get_template("intent_classifier")
    start = time.perf_counter()
    for _ in range(500):
        registry.get_template("intent_classifier")
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_lookup_ms = elapsed_ms / 500
    assert per_lookup_ms < 1.0, f"Registry lookup took {per_lookup_ms:.3f} ms (expected < 1 ms)"
