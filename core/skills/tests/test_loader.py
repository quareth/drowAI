"""Tests for secure, bounded built-in skill package loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.skills.errors import SkillLoadError, SkillParseError, SkillValidationError
from core.skills.loader import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_FRONTMATTER_CHARACTERS,
    MAX_SKILL_METADATA_ENTRIES,
    MAX_SKILL_METADATA_KEY_CHARACTERS,
    MAX_SKILL_METADATA_VALUE_CHARACTERS,
    SkillLoader,
)


def test_loader_validates_and_digests_selectable_package(tmp_path: Path) -> None:
    package = _write_skill(tmp_path, "network_reconnaissance")

    first = SkillLoader(builtin_root=tmp_path).load(package)
    second = SkillLoader(builtin_root=tmp_path).load(package)

    assert first.skill_id == "network_reconnaissance"
    assert first.metadata.version == "1"
    assert first.activation.activation == "selectable"
    assert first.activation.agent_ids == ("pathfinder",)
    assert first.digest == second.digest
    assert len(first.digest) == 64
    assert first.source == "network_reconnaissance/SKILL.md"


def test_loader_accepts_mandatory_package(tmp_path: Path) -> None:
    package = _write_skill(
        tmp_path,
        "baseline_recon",
        activation="mandatory",
        agent_ids="pathfinder,webweaver",
    )

    skill = SkillLoader(builtin_root=tmp_path).load(package)

    assert skill.activation.activation == "mandatory"
    assert skill.activation.agent_ids == ("pathfinder", "webweaver")


def test_loader_rejects_unknown_frontmatter_field(tmp_path: Path) -> None:
    package = _write_skill(tmp_path, "network_reconnaissance", extra="allowed-tools: shell")

    with pytest.raises(SkillValidationError, match="unknown top-level fields"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_malformed_frontmatter(tmp_path: Path) -> None:
    package = tmp_path / "broken"
    package.mkdir()
    (package / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")

    with pytest.raises(SkillParseError):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_package_symlink(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    actual = _write_skill(actual_root, "network_reconnaissance")
    link_root = tmp_path / "builtin"
    link_root.mkdir()
    link = link_root / "network_reconnaissance"
    link.symlink_to(actual, target_is_directory=True)

    with pytest.raises(SkillLoadError, match="symlink"):
        SkillLoader(builtin_root=link_root).load(link)


def test_loader_rejects_entrypoint_symlink(tmp_path: Path) -> None:
    package = tmp_path / "network_reconnaissance"
    package.mkdir()
    target = tmp_path / "outside.md"
    target.write_text("---\nname: outside\n---\nbody\n", encoding="utf-8")
    (package / "SKILL.md").symlink_to(target)

    with pytest.raises(SkillLoadError, match="entrypoint must not be a symlink"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_nested_or_escaping_package(tmp_path: Path) -> None:
    root = tmp_path / "builtin"
    root.mkdir()
    outside = _write_skill(tmp_path / "outside", "network_reconnaissance")

    with pytest.raises(SkillLoadError, match="immediate child"):
        SkillLoader(builtin_root=root).load(outside)


def test_loader_rejects_multiple_activation_values(tmp_path: Path) -> None:
    package = _write_skill(
        tmp_path,
        "network_reconnaissance",
        activation="mandatory,selectable",
    )

    with pytest.raises(SkillValidationError, match="exactly one mode"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_missing_agent_ids(tmp_path: Path) -> None:
    package = _write_skill(tmp_path, "network_reconnaissance", agent_ids="")

    with pytest.raises(SkillValidationError, match="at least one compatible agent id"):
        SkillLoader(builtin_root=tmp_path).load(package)


@pytest.mark.parametrize(
    "legacy_key",
    (
        "priority",
        "agent-kinds",
        "trigger-tool-ids",
        "trigger-capability-ids",
        "trigger-capability-families",
    ),
)
def test_loader_rejects_retired_policy_metadata(
    tmp_path: Path,
    legacy_key: str,
) -> None:
    package = _write_skill(
        tmp_path,
        "network_reconnaissance",
        metadata_extra=f'  {legacy_key}: "retired"',
    )

    with pytest.raises(SkillValidationError, match="retired skill policy keys"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_unsafe_yaml_tags(tmp_path: Path) -> None:
    package = tmp_path / "network_reconnaissance"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: !!python/object/apply:os.system ['false']\n---\nbody\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillParseError, match="invalid YAML"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_oversized_body(tmp_path: Path) -> None:
    package = _write_skill(tmp_path, "network_reconnaissance")
    entrypoint = package / "SKILL.md"
    entrypoint.write_text(
        entrypoint.read_text(encoding="utf-8") + ("x" * 40_001),
        encoding="utf-8",
    )

    with pytest.raises(SkillValidationError, match="character limit"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_oversized_file_before_parsing(tmp_path: Path) -> None:
    package = tmp_path / "network_reconnaissance"
    package.mkdir()
    (package / "SKILL.md").write_bytes(b"x" * (MAX_SKILL_FILE_BYTES + 1))

    with pytest.raises(SkillLoadError, match="byte limit"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_oversized_frontmatter_before_yaml_parsing(
    tmp_path: Path,
) -> None:
    package = tmp_path / "network_reconnaissance"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\npadding: "
        + ("x" * MAX_SKILL_FRONTMATTER_CHARACTERS)
        + "\n---\nbody\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillParseError, match="frontmatter exceeds character limit"):
        SkillLoader(builtin_root=tmp_path).load(package)


def test_loader_rejects_too_many_metadata_entries(tmp_path: Path) -> None:
    extra_metadata = "\n".join(
        f'  extra_{index}: "value"' for index in range(MAX_SKILL_METADATA_ENTRIES)
    )
    package = _write_skill(
        tmp_path,
        "network_reconnaissance",
        metadata_extra=extra_metadata,
    )

    with pytest.raises(SkillValidationError, match="metadata exceeds entry limit"):
        SkillLoader(builtin_root=tmp_path).load(package)


@pytest.mark.parametrize(
    ("metadata_extra", "message"),
    (
        (
            f'  {"k" * (MAX_SKILL_METADATA_KEY_CHARACTERS + 1)}: "value"',
            "metadata key exceeds character limit",
        ),
        (
            '  extra: "'
            + ("v" * (MAX_SKILL_METADATA_VALUE_CHARACTERS + 1))
            + '"',
            "metadata.extra exceeds character limit",
        ),
    ),
)
def test_loader_rejects_oversized_metadata_fields(
    tmp_path: Path,
    metadata_extra: str,
    message: str,
) -> None:
    package = _write_skill(
        tmp_path,
        "network_reconnaissance",
        metadata_extra=metadata_extra,
    )

    with pytest.raises(SkillValidationError, match=message):
        SkillLoader(builtin_root=tmp_path).load(package)


def _write_skill(
    root: Path,
    name: str,
    *,
    activation: str = "selectable",
    agent_ids: str = "pathfinder",
    extra: str = "",
    metadata_extra: str = "",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    package = root / name
    package.mkdir()
    agent_ids_line = f'  agent-ids: "{agent_ids}"\n' if agent_ids else ""
    (package / "SKILL.md").write_text(
        f"""---
name: {name}
description: Bounded network discovery guidance.
{extra}
metadata:
  version: "1"
  activation: "{activation}"
{agent_ids_line}{metadata_extra}
---

# Guidance

Use bounded discovery.
""",
        encoding="utf-8",
    )
    return package
