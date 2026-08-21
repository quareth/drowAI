"""Tests for deterministic discovery and digest-checked skill materialization."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.skills.contracts import ResolvedSkillRef
from core.skills.errors import SkillLoadError, SkillRegistryError
from core.skills.registry import SkillRegistry, load_skill_registry


def test_registry_loads_zero_registration_package(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    registry = load_skill_registry(tmp_path)

    skill = registry.require("network_reconnaissance")

    assert skill.metadata.description == "Fixture discovery guidance."
    assert registry.skills() == (skill,)


def test_registry_rejects_directories_without_skill_entrypoint(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()

    with pytest.raises(SkillLoadError, match="unable to read skill entrypoint"):
        load_skill_registry(tmp_path)


def test_registry_rejects_missing_builtin_root(tmp_path: Path) -> None:
    with pytest.raises(SkillLoadError, match="skill root does not exist"):
        load_skill_registry(tmp_path / "missing")


def test_registry_materializes_only_matching_version_and_digest(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path)
    registry = load_skill_registry(tmp_path)
    skill = registry.require("network_reconnaissance")
    ref = ResolvedSkillRef(
        skill_id=skill.skill_id,
        version=skill.metadata.version,
        digest=skill.digest,
        reasons=("mandatory",),
    )

    assert registry.materialize((ref,)) == (skill,)

    changed = ref.model_copy(update={"digest": "0" * 64})
    with pytest.raises(SkillRegistryError, match="changed after resolution"):
        registry.materialize((changed,))


def test_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    skill = load_skill_registry(tmp_path).require("network_reconnaissance")

    with pytest.raises(SkillRegistryError, match="duplicate skill id"):
        SkillRegistry((skill, skill))


def _write_skill(root: Path) -> None:
    package = root / "network_reconnaissance"
    package.mkdir()
    (package / "SKILL.md").write_text(
        """---
name: network_reconnaissance
description: Fixture discovery guidance.
metadata:
  activation: "mandatory"
  agent-ids: "pathfinder"
---

# Fixture

Use bounded discovery.
""",
        encoding="utf-8",
    )
