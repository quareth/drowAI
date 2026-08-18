"""Immutable lookup and digest-checked materialization for built-in skills."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from core.skills.contracts import LoadedSkill, ResolvedSkillRef
from core.skills.discovery import discover_skill_packages
from core.skills.errors import SkillRegistryError
from core.skills.loader import SkillLoader


DEFAULT_BUILTIN_SKILL_ROOT = Path(__file__).resolve().parent / "builtin"


class SkillRegistry:
    """Read-only registry of eagerly validated built-in packages."""

    def __init__(self, skills: Iterable[LoadedSkill]) -> None:
        ordered = tuple(skills)
        by_id: dict[str, LoadedSkill] = {}
        digests: dict[str, str] = {}
        for skill in ordered:
            if skill.skill_id in by_id:
                raise SkillRegistryError(f"duplicate skill id: {skill.skill_id}")
            prior_id = digests.get(skill.digest)
            if prior_id is not None and prior_id != skill.skill_id:
                raise SkillRegistryError(
                    f"skill digest collision between {prior_id} and {skill.skill_id}"
                )
            by_id[skill.skill_id] = skill
            digests[skill.digest] = skill.skill_id
        self._skills = tuple(sorted(ordered, key=lambda skill: skill.skill_id))
        self._by_id: Mapping[str, LoadedSkill] = MappingProxyType(by_id)

    def skills(self) -> tuple[LoadedSkill, ...]:
        """Return loaded skills in deterministic identifier order."""

        return self._skills

    def get(self, skill_id: str) -> LoadedSkill | None:
        """Return one package by canonical identifier."""

        return self._by_id.get(str(skill_id or "").strip().lower())

    def require(self, skill_id: str) -> LoadedSkill:
        """Return one package or raise a registry configuration error."""

        skill = self.get(skill_id)
        if skill is None:
            raise SkillRegistryError(f"unknown built-in skill: {skill_id}")
        return skill

    def materialize(self, refs: Sequence[ResolvedSkillRef]) -> tuple[LoadedSkill, ...]:
        """Verify version and digest before returning trusted package bodies."""

        materialized: list[LoadedSkill] = []
        for ref in refs:
            skill = self.require(ref.skill_id)
            if skill.metadata.version != ref.version or skill.digest != ref.digest:
                raise SkillRegistryError(
                    f"built-in skill changed after resolution: {ref.skill_id}"
                )
            materialized.append(skill)
        return tuple(materialized)


def load_skill_registry(
    builtin_root: Path | str = DEFAULT_BUILTIN_SKILL_ROOT,
) -> SkillRegistry:
    """Discover and fail-fast load every built-in package."""

    root = Path(builtin_root)
    loader = SkillLoader(builtin_root=root)
    return SkillRegistry(loader.load(path) for path in discover_skill_packages(root))


_SKILL_REGISTRY: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    """Return the single process-local built-in skill registry."""

    global _SKILL_REGISTRY
    if _SKILL_REGISTRY is None:
        _SKILL_REGISTRY = load_skill_registry()
    return _SKILL_REGISTRY


__all__ = [
    "DEFAULT_BUILTIN_SKILL_ROOT",
    "SkillRegistry",
    "get_skill_registry",
    "load_skill_registry",
]
