"""Tests for the canonical subagent handoff model, normalization, and schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.graph.nodes.post_tool_reasoning.models import (
    AgentHandoffEntry as PtrAgentHandoffEntry,
)
from agent.subagents.handoff import (
    AgentHandoffEntry,
    agent_handoff_entry_json_schema,
    normalize_agent_handoff_entries,
    normalize_agent_handoff_entry,
)


def test_model_preserves_authored_non_blank_values() -> None:
    entry = AgentHandoffEntry(
        agent_handoff="required",
        subagent=" Pathfinder ",
        objective=" Enumerate the approved target. ",
        skill_ids=("network_reconnaissance",),
    )

    assert entry.subagent == " Pathfinder "
    assert entry.objective == " Enumerate the approved target. "


def test_ptr_models_reexport_the_canonical_handoff_model() -> None:
    assert PtrAgentHandoffEntry is AgentHandoffEntry


@pytest.mark.parametrize(
    "payload",
    [
        {
            "agent_handoff": "required",
            "subagent": " ",
            "objective": "Enumerate the approved target.",
        },
        {
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": " ",
        },
        {
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": "Enumerate the approved target.",
            "unexpected": True,
        },
    ],
)
def test_model_rejects_blank_or_extra_fields(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentHandoffEntry.model_validate(payload)


def test_single_entry_normalizer_trims_and_lowercases_boundary_values() -> None:
    assert normalize_agent_handoff_entry(
        {
            "agent_handoff": " Required ",
            "subagent": " PathFinder ",
            "objective": "  Enumerate the approved target.  ",
            "skill_ids": [" Network_Reconnaissance ", "network_reconnaissance"],
        }
    ) == {
        "agent_handoff": "required",
        "subagent": "pathfinder",
        "objective": "Enumerate the approved target.",
        "skill_ids": ["network_reconnaissance"],
    }


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"agent_handoff": "optional", "subagent": "pathfinder", "objective": "x"},
        {"agent_handoff": "required", "subagent": 1, "objective": "x"},
        {"agent_handoff": "required", "subagent": "pathfinder", "objective": " "},
    ],
)
def test_single_entry_normalizer_rejects_malformed_values(value: object) -> None:
    assert normalize_agent_handoff_entry(value) == {}


def test_skill_id_limit_applies_before_deduplication() -> None:
    payload = {
        "agent_handoff": "required",
        "subagent": "pathfinder",
        "objective": "Enumerate the approved target.",
        "skill_ids": ["same", "same", "same", "same", "same", "same"],
    }

    assert normalize_agent_handoff_entry(payload) == {}
    with pytest.raises(ValidationError):
        AgentHandoffEntry.model_validate(payload)


@pytest.mark.parametrize("skill_id", ("a" * 65, "network-", "network--recon"))
def test_skill_ids_reject_noncanonical_length_and_hyphen_placement(
    skill_id: str,
) -> None:
    payload = {
        "agent_handoff": "required",
        "subagent": "pathfinder",
        "objective": "Enumerate the approved target.",
        "skill_ids": [skill_id],
    }

    assert normalize_agent_handoff_entry(payload) == {}
    with pytest.raises(ValidationError):
        AgentHandoffEntry.model_validate(payload)


def test_collection_normalizer_preserves_order_deduplicates_and_bounds() -> None:
    entries = normalize_agent_handoff_entries(
        [
            {
                "agent_handoff": "required",
                "subagent": " Pathfinder ",
                "objective": " First objective. ",
                "skill_ids": [],
            },
            {
                "agent_handoff": "required",
                "subagent": "pathfinder",
                "objective": "First objective.",
                "skill_ids": [],
            },
            {
                "agent_handoff": "required",
                "subagent": "cartographer",
                "objective": "Second objective.",
                "skill_ids": ["network_reconnaissance"],
            },
            {
                "agent_handoff": "required",
                "subagent": "reviewer",
                "objective": "Third objective.",
                "skill_ids": [],
            },
        ],
        max_handoffs=2,
    )

    assert entries == (
        {
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": "First objective.",
            "skill_ids": [],
        },
        {
            "agent_handoff": "required",
            "subagent": "cartographer",
            "objective": "Second objective.",
            "skill_ids": ["network_reconnaissance"],
        },
    )


def test_collection_normalizer_filters_invalid_entries_in_best_effort_mode() -> None:
    entries = normalize_agent_handoff_entries(
        [
            {"agent_handoff": "optional", "subagent": "pathfinder", "objective": "x"},
            {
                "agent_handoff": "required",
                "subagent": "pathfinder",
                "objective": "ok",
                "skill_ids": [],
            },
            "malformed",
        ]
    )

    assert entries == (
        {
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": "ok",
            "skill_ids": [],
        },
    )


@pytest.mark.parametrize(
    "value",
    [
        "malformed",
        [{"agent_handoff": "optional", "subagent": "pathfinder", "objective": "x"}],
        [
            {
                "agent_handoff": "required",
                "subagent": "pathfinder",
                "objective": "x",
                "unexpected": True,
            }
        ],
    ],
)
def test_collection_normalizer_rejects_invalid_values_in_strict_mode(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="invalid_handoff_plan"):
        normalize_agent_handoff_entries(value, reject_invalid=True)


def test_schema_matches_model_shape_and_registry_enumeration() -> None:
    schema = agent_handoff_entry_json_schema(
        (" Pathfinder ", "cartographer", "pathfinder", " ")
    )

    assert schema["type"] == "object"
    assert schema["required"] == [
        "agent_handoff",
        "subagent",
        "objective",
        "skill_ids",
    ]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["agent_handoff"] == {
        "type": "string",
        "enum": ["required"],
    }
    assert schema["properties"]["subagent"] == {
        "type": "string",
        "minLength": 1,
        "enum": ["pathfinder", "cartographer"],
    }
    assert schema["properties"]["objective"] == {
        "type": "string",
        "minLength": 1,
    }
    assert schema["properties"]["skill_ids"]["maxItems"] == 5
    skill_id_schema = schema["properties"]["skill_ids"]["items"]
    assert skill_id_schema["maxLength"] == 64
    assert all(
        operator not in skill_id_schema["pattern"]
        for operator in ("(?=", "(?!", "(?<=", "(?<!")
    )
