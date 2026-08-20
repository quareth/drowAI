"""Pure direct-agent eligibility and final selection policy for skills."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from core.skills.contracts import (
    LoadedSkill,
    MAX_REQUESTED_SKILLS,
    RejectedSkillRequest,
    ResolvedSkillRef,
    SkillResolution,
)
from core.skills.errors import SkillResolutionError
from core.skills.identifiers import normalize_skill_id


MAX_SELECTED_SKILLS = MAX_REQUESTED_SKILLS
MAX_TOTAL_ESTIMATED_TOKENS = 12_000


def estimate_skill_tokens(text: str) -> int:
    """Estimate guidance tokens using the canonical four-characters heuristic."""

    return (len(text) + 3) // 4


def is_agent_compatible(skill: LoadedSkill, agent_id: str) -> bool:
    """Return whether the skill declares compatibility with the agent."""

    return str(agent_id or "").strip().lower() in skill.activation.agent_ids


def eligible_selectable_skills(
    skills: Iterable[LoadedSkill],
    agent_id: str,
) -> tuple[LoadedSkill, ...]:
    """Return compatible selectable packages in deterministic identifier order."""

    return tuple(
        sorted(
            (
                skill
                for skill in skills
                if skill.activation.activation == "selectable"
                and is_agent_compatible(skill, agent_id)
            ),
            key=lambda skill: skill.skill_id,
        )
    )


def compatible_mandatory_skills(
    skills: Iterable[LoadedSkill],
    agent_id: str,
) -> tuple[LoadedSkill, ...]:
    """Return compatible mandatory packages in deterministic identifier order."""

    return tuple(
        sorted(
            (
                skill
                for skill in skills
                if skill.activation.activation == "mandatory"
                and is_agent_compatible(skill, agent_id)
            ),
            key=lambda skill: skill.skill_id,
        )
    )


def resolve_skills(
    skills: Sequence[LoadedSkill],
    agent_id: str,
    requested_skill_ids: Sequence[str] = (),
    *,
    max_total_estimated_tokens: int = MAX_TOTAL_ESTIMATED_TOKENS,
) -> SkillResolution:
    """Resolve compatible mandatory skills and valid parent-selected packages."""

    normalized_agent_id = str(agent_id or "").strip().lower()
    by_id = {skill.skill_id: skill for skill in skills}
    selected_ids: list[str] = []
    rejected: list[RejectedSkillRequest] = []

    mandatory_ids = [
        skill.skill_id
        for skill in compatible_mandatory_skills(skills, normalized_agent_id)
    ]
    selected_ids.extend(mandatory_ids)
    mandatory_tokens = sum(
        estimate_skill_tokens(by_id[skill_id].body) for skill_id in mandatory_ids
    )
    if mandatory_tokens > max_total_estimated_tokens:
        raise SkillResolutionError("mandatory skill guidance exceeds budget")

    seen_requests: set[str] = set()
    admitted_requests = 0
    selected_tokens = mandatory_tokens
    for raw_id in requested_skill_ids:
        skill_id = _normalize_requested_id(raw_id)
        if skill_id in seen_requests:
            rejected.append(RejectedSkillRequest(skill_id=skill_id, code="duplicate_request"))
            continue
        seen_requests.add(skill_id)
        if admitted_requests >= MAX_SELECTED_SKILLS:
            rejected.append(
                RejectedSkillRequest(skill_id=skill_id, code="selected_count_exceeded")
            )
            continue
        admitted_requests += 1

        skill = by_id.get(skill_id)
        if skill is None:
            rejected.append(RejectedSkillRequest(skill_id=skill_id, code="unknown_skill"))
            continue
        if skill.activation.activation != "selectable":
            rejected.append(RejectedSkillRequest(skill_id=skill_id, code="not_selectable"))
            continue
        if not is_agent_compatible(skill, normalized_agent_id):
            rejected.append(
                RejectedSkillRequest(skill_id=skill_id, code="incompatible_agent")
            )
            continue

        if skill_id not in selected_ids:
            prospective_tokens = selected_tokens + estimate_skill_tokens(skill.body)
            if prospective_tokens > max_total_estimated_tokens:
                rejected.append(
                    RejectedSkillRequest(
                        skill_id=skill_id,
                        code="instruction_budget_exceeded",
                    )
                )
                continue
            selected_tokens = prospective_tokens
            selected_ids.append(skill_id)

    selected = tuple(
        ResolvedSkillRef(
            skill_id=skill_id,
            version=by_id[skill_id].metadata.version,
            digest=by_id[skill_id].digest,
            reasons=(
                ("mandatory",)
                if by_id[skill_id].activation.activation == "mandatory"
                else ("agent_selected",)
            ),
        )
        for skill_id in selected_ids
    )
    return SkillResolution(
        selected=selected,
        rejected_requests=tuple(rejected),
        estimated_tokens=selected_tokens,
    )


def _normalize_requested_id(value: str) -> str:
    try:
        return normalize_skill_id(value)
    except ValueError:
        return str(value or "").strip().lower()


__all__ = [
    "MAX_SELECTED_SKILLS",
    "MAX_TOTAL_ESTIMATED_TOKENS",
    "compatible_mandatory_skills",
    "eligible_selectable_skills",
    "estimate_skill_tokens",
    "is_agent_compatible",
    "resolve_skills",
]
