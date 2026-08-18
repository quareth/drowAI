"""Immutable contracts for built-in skill packages and runtime selection."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


MAX_REQUESTED_SKILLS = 5

SkillActivationMode = Literal[
    "mandatory",
    "selectable",
]
SkillSelectionReason = Literal[
    "mandatory",
    "agent_selected",
]
SkillRejectionCode = Literal[
    "unknown_skill",
    "not_selectable",
    "incompatible_agent",
    "duplicate_request",
    "selected_count_exceeded",
    "instruction_budget_exceeded",
]


class _FrozenContract(BaseModel):
    """Reject unknown fields and mutation for checkpoint-safe values."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillMetadata(_FrozenContract):
    """Standard model-visible metadata from one skill package."""

    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    version: str = "1"


class SkillActivationPolicy(_FrozenContract):
    """Deterministic activation and compatibility policy for one skill."""

    activation: SkillActivationMode
    agent_ids: tuple[str, ...] = Field(min_length=1)


class LoadedSkill(_FrozenContract):
    """Validated package content with a stable package-relative identity."""

    metadata: SkillMetadata
    activation: SkillActivationPolicy
    body: str
    source: str
    digest: str

    @property
    def skill_id(self) -> str:
        """Return the canonical package identifier."""

        return self.metadata.name


class SkillCatalogEntry(_FrozenContract):
    """Minimal model-visible projection of an eligible skill."""

    skill_id: str
    description: str


class SubagentSkillCatalog(_FrozenContract):
    """Bounded eligible skill entries for one subagent definition."""

    agent_id: str
    mandatory_skills: tuple[SkillCatalogEntry, ...] = ()
    selectable_skills: tuple[SkillCatalogEntry, ...] = ()


class ResolvedSkillRef(_FrozenContract):
    """Body-free package reference persisted in subagent runtime state."""

    skill_id: str
    version: str
    digest: str
    reasons: tuple[SkillSelectionReason, ...]


class RejectedSkillRequest(_FrozenContract):
    """Safe diagnostic for one omitted optional model request."""

    skill_id: str
    code: SkillRejectionCode


class SkillResolution(_FrozenContract):
    """Final body-free selection and bounded optional-request diagnostics."""

    selected: tuple[ResolvedSkillRef, ...] = ()
    rejected_requests: tuple[RejectedSkillRequest, ...] = ()
    estimated_tokens: int = Field(default=0, ge=0)


__all__ = [
    "LoadedSkill",
    "MAX_REQUESTED_SKILLS",
    "RejectedSkillRequest",
    "ResolvedSkillRef",
    "SkillActivationMode",
    "SkillActivationPolicy",
    "SkillCatalogEntry",
    "SkillMetadata",
    "SkillRejectionCode",
    "SkillResolution",
    "SkillSelectionReason",
    "SubagentSkillCatalog",
]
