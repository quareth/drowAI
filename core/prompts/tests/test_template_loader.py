"""Unit tests for the prompt template loader and registry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.prompts.loader import TemplateLoader
from core.prompts.registry import PromptRegistry


def test_template_loader_load_and_render(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    root.mkdir()
    (root / "hello.txt").write_text("Hello {name}!", encoding="utf-8")

    loader = TemplateLoader(root)
    loader.cache_clear()

    assert loader.load("hello.txt") == "Hello {name}!"
    assert loader.render("hello.txt", name="world") == "Hello world!"


def test_template_loader_lru_cache_hits(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    root.mkdir()
    (root / "cached.txt").write_text("cache me", encoding="utf-8")

    loader = TemplateLoader(root)
    loader.cache_clear()

    info_before = loader.cache_info()
    loader.load("cached.txt")
    loader.load("cached.txt")
    info_after = loader.cache_info()

    assert info_after["template_files"].hits == info_before["template_files"].hits + 1


def test_template_loader_latest_version(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    (root / "family" / "v1").mkdir(parents=True)
    (root / "family" / "v2").mkdir(parents=True)
    (root / "family" / "latest.txt").write_text("v2\n", encoding="utf-8")
    (root / "family" / "v2" / "prompt.txt").write_text("new", encoding="utf-8")

    loader = TemplateLoader(root)
    assert loader.load_latest_version("family", "prompt.txt") == "new"


def test_template_loader_latest_manifest_cache_and_invalidation(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    (root / "family" / "v1").mkdir(parents=True)
    (root / "family" / "v2").mkdir(parents=True)
    (root / "family" / "latest.txt").write_text("v1\n", encoding="utf-8")
    (root / "family" / "v1" / "prompt.txt").write_text("old", encoding="utf-8")
    (root / "family" / "v2" / "prompt.txt").write_text("new", encoding="utf-8")

    loader = TemplateLoader(root)
    loader.cache_clear()

    assert loader.load_latest_version("family", "prompt.txt") == "old"
    (root / "family" / "latest.txt").write_text("v2\n", encoding="utf-8")

    # Cached manifest remains stable until explicit invalidation.
    assert loader.load_latest_version("family", "prompt.txt") == "old"
    loader.cache_clear()
    assert loader.load_latest_version("family", "prompt.txt") == "new"


def test_prompt_registry_version_resolution(tmp_path: Path) -> None:
    templates_root = tmp_path / "templates"
    (templates_root / "chat" / "v1").mkdir(parents=True)
    (templates_root / "chat" / "latest.txt").write_text("v1\n", encoding="utf-8")
    (templates_root / "chat" / "v1" / "system.txt").write_text("hi", encoding="utf-8")

    registry = PromptRegistry(templates_root=templates_root)
    registry.register_template_id("chat_example_system", family="chat", filename="system.txt")

    assert registry.get_template("chat_example_system") == "hi"
    assert registry.get_template("chat_example_system", version="latest") == "hi"

    with pytest.raises(KeyError):
        registry.get_template("missing_template_id")


def test_prompt_registry_exposes_simple_chat_template(tmp_path: Path) -> None:
    templates_root = tmp_path / "templates"
    (templates_root / "simple_chat" / "v1").mkdir(parents=True)
    (templates_root / "simple_chat" / "latest.txt").write_text("v1\n", encoding="utf-8")
    (templates_root / "simple_chat" / "v1" / "system.txt").write_text(
        "DrowAI chat system prompt",
        encoding="utf-8",
    )

    registry = PromptRegistry(templates_root=templates_root)
    assert registry.get_template("simple_chat_system") == "DrowAI chat system prompt"


def test_template_loader_raises_for_missing_template(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    root.mkdir()

    loader = TemplateLoader(root)
    with pytest.raises(FileNotFoundError):
        loader.load("missing.txt")


def test_template_loader_raises_for_empty_latest_manifest(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    (root / "family").mkdir(parents=True)
    (root / "family" / "latest.txt").write_text("\n", encoding="utf-8")

    loader = TemplateLoader(root)
    with pytest.raises(ValueError):
        loader.load_latest_version("family", "prompt.txt")


def test_prompt_registry_unknown_builder_raises_keyerror(tmp_path: Path) -> None:
    templates_root = tmp_path / "templates"
    (templates_root / "intent" / "v1").mkdir(parents=True)
    (templates_root / "intent" / "latest.txt").write_text("v1\n", encoding="utf-8")
    (templates_root / "intent" / "v1" / "intent_classifier.txt").write_text(
        "classifier",
        encoding="utf-8",
    )
    (templates_root / "intent" / "v1" / "prompt_template.txt").write_text(
        "template",
        encoding="utf-8",
    )

    registry = PromptRegistry(templates_root=templates_root)
    with pytest.raises(KeyError):
        registry.get_chat_builder("unknown_builder")


def test_prompt_registry_get_chat_builder_is_thread_safe(tmp_path: Path) -> None:
    templates_root = tmp_path / "templates"
    (templates_root / "intent" / "v1").mkdir(parents=True)
    (templates_root / "intent" / "latest.txt").write_text("v1\n", encoding="utf-8")
    (templates_root / "intent" / "v1" / "intent_classifier.txt").write_text(
        "classifier",
        encoding="utf-8",
    )
    (templates_root / "intent" / "v1" / "prompt_template.txt").write_text(
        "template",
        encoding="utf-8",
    )

    registry = PromptRegistry(templates_root=templates_root)
    created = {"count": 0}

    class _Builder:
        pass

    def _factory() -> object:
        created["count"] += 1
        return _Builder()

    registry.register_chat_builder("thread_test_builder", _factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: registry.get_chat_builder("thread_test_builder"), range(32)))

    assert created["count"] == 1
    first = results[0]
    assert all(result is first for result in results)
