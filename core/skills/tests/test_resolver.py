"""Tests for deterministic mandatory and parent-selected skill policy."""

from __future__ import annotations

import pytest

from core.skills.contracts import LoadedSkill, SkillActivationPolicy, SkillMetadata
from core.skills.errors import SkillResolutionError
from core.skills.resolver import eligible_selectable_skills, resolve_skills


def test_resolver_orders_mandatory_by_id_then_selected_by_request_order() -> None:
    mandatory_b = _skill("mandatory_b", activation="mandatory")
    mandatory_a = _skill("mandatory_a", activation="mandatory")
    selectable_a = _skill("selectable_a", activation="selectable")
    selectable_b = _skill("selectable_b", activation="selectable")

    resolution = resolve_skills(
        (selectable_b, mandatory_b, selectable_a, mandatory_a),
        "pathfinder",
        ("selectable_b", "selectable_a"),
    )

    assert resolution.rejected_requests == ()
    assert tuple(ref.skill_id for ref in resolution.selected) == (
        "mandatory_a",
        "mandatory_b",
        "selectable_b",
        "selectable_a",
    )
    assert tuple(ref.reasons for ref in resolution.selected) == (
        ("mandatory",),
        ("mandatory",),
        ("agent_selected",),
        ("agent_selected",),
    )


def test_catalog_returns_only_compatible_selectable_skills_by_id() -> None:
    selectable_b = _skill("selectable_b", activation="selectable")
    selectable_a = _skill("selectable_a", activation="selectable")
    mandatory = _skill("mandatory", activation="mandatory")
    incompatible = _skill(
        "incompatible",
        activation="selectable",
        agent_ids=("other_agent",),
    )

    assert eligible_selectable_skills(
        (selectable_b, mandatory, incompatible, selectable_a),
        "pathfinder",
    ) == (selectable_a, selectable_b)


def test_required_guidance_is_not_displaced_by_selected_budget() -> None:
    required = _skill("required", activation="mandatory", body="r" * 40)
    selected = _skill("selected", activation="selectable", body="s" * 40)

    resolution = resolve_skills(
        (required, selected),
        "pathfinder",
        ("selected",),
        max_total_estimated_tokens=10,
    )

    assert tuple(ref.skill_id for ref in resolution.selected) == ("required",)
    assert resolution.rejected_requests[0].code == "instruction_budget_exceeded"


def test_mandatory_guidance_overflow_fails_closed() -> None:
    required = _skill("required", activation="mandatory", body="r" * 44)

    with pytest.raises(SkillResolutionError, match="exceeds budget"):
        resolve_skills(
            (required,),
            "pathfinder",
            max_total_estimated_tokens=10,
        )


def test_selected_requests_report_stable_rejection_codes() -> None:
    mandatory = _skill("mandatory", activation="mandatory")
    incompatible = _skill(
        "incompatible",
        activation="selectable",
        agent_ids=("other_agent",),
    )
    optional = tuple(
        _skill(f"optional_{index}", activation="selectable")
        for index in range(5)
    )

    resolution = resolve_skills(
        (mandatory, incompatible, *optional),
        "pathfinder",
        (
            "missing",
            "mandatory",
            "incompatible",
            "optional_0",
            "optional_0",
            "optional_1",
            "optional_2",
        ),
    )

    assert tuple(item.code for item in resolution.rejected_requests) == (
        "unknown_skill",
        "not_selectable",
        "incompatible_agent",
        "duplicate_request",
        "selected_count_exceeded",
    )
    assert tuple(ref.skill_id for ref in resolution.selected) == (
        "mandatory",
        "optional_0",
        "optional_1",
    )


def test_mandatory_skills_do_not_consume_selected_skill_slots() -> None:
    mandatory = tuple(
        _skill(f"mandatory_{index}", activation="mandatory")
        for index in range(3)
    )
    selectable = tuple(
        _skill(f"selectable_{index}", activation="selectable")
        for index in range(5)
    )

    resolution = resolve_skills(
        (*mandatory, *selectable),
        "pathfinder",
        tuple(skill.skill_id for skill in selectable),
    )

    assert resolution.rejected_requests == ()
    assert len(resolution.selected) == 8
    assert tuple(ref.skill_id for ref in resolution.selected[:3]) == (
        "mandatory_0",
        "mandatory_1",
        "mandatory_2",
    )
    assert tuple(ref.skill_id for ref in resolution.selected[3:]) == tuple(
        skill.skill_id for skill in selectable
    )


def _skill(
    skill_id: str,
    *,
    activation: str,
    agent_ids: tuple[str, ...] = ("pathfinder",),
    body: str = "bounded guidance",
) -> LoadedSkill:
    return LoadedSkill(
        metadata=SkillMetadata(
            name=skill_id,
            description=f"{skill_id} guidance",
            version="1",
        ),
        activation=SkillActivationPolicy(
            activation=activation,
            agent_ids=agent_ids,
        ),
        body=body,
        source=f"{skill_id}/SKILL.md",
        digest=(skill_id.encode().hex() + "0" * 64)[:64],
    )
