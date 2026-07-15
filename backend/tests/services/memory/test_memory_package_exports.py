"""Guard the semantic memory package import/export surface."""

from __future__ import annotations


def test_memory_package_exports_symbols() -> None:
    from backend.services.memory import (  # noqa: PLC0415 - import in test guards package surface
        MemoryExtractionService,
        MemoryStore,
        MemoryTier,
        enqueue_memory_extraction,
    )

    assert MemoryExtractionService is not None
    assert MemoryStore is not None
    assert MemoryTier.USER_PROFILE.value == "user_profile"
    assert callable(enqueue_memory_extraction)
