"""Tests for the canonical subagent registration and classifier projection."""

from __future__ import annotations

import pytest

from backend.services.agent_runs.subagent_registry import (
    SubagentRegistry,
    SubagentSpec,
    get_subagent_registry,
)
from core.prompts.builders.intent_classifier import build_classifier_system_prompt


def test_default_registry_exposes_pathfinder_as_the_only_available_subagent() -> None:
    registry = get_subagent_registry()

    assert registry.names() == ("pathfinder",)
    pathfinder = registry.require("pathfinder")
    assert pathfinder.agent_id == "pathfinder"
    assert pathfinder.agent_kind == "recon"
    assert pathfinder.dispatch_branch == "subagent"
    assert pathfinder.requires_resolved_target is True
    assert pathfinder.max_active_runs_per_task == 1
    assert pathfinder.supported_task_categories == (
        "host_discovery",
        "port_scanning",
        "service_enumeration",
    )


def test_classifier_catalog_is_derived_from_available_registry_specs() -> None:
    registry = get_subagent_registry()

    assert registry.classifier_catalog() == (
        {
            "name": "pathfinder",
            "agent_id": "pathfinder",
            "display_name": "Pathfinder",
            "purpose": (
                "Perform bounded network reconnaissance and return concise "
                "evidence to the main agent."
            ),
            "ownership_boundary": (
                "Own host discovery, port scanning, and service enumeration "
                "only; do not exploit, authenticate, modify targets, or produce "
                "the user's final answer."
            ),
            "supported_task_categories": (
                "host_discovery",
                "port_scanning",
                "service_enumeration",
            ),
            "excluded_task_categories": (
                "exploitation",
                "credential_attacks",
                "phishing",
                "payload_delivery",
                "privilege_escalation",
                "reporting",
            ),
            "max_active_runs_per_task": 1,
            "requires_resolved_target": True,
        },
    )


def test_registry_rejects_duplicate_names() -> None:
    spec = get_subagent_registry().require("pathfinder")

    with pytest.raises(ValueError, match="duplicate subagent name"):
        SubagentRegistry((spec, spec))


def test_disabled_specs_are_not_projected_to_classifier() -> None:
    disabled = SubagentSpec(
        name="disabled_pathfinder",
        agent_id="disabled_pathfinder",
        display_name="Disabled Pathfinder",
        agent_kind="recon",
        dispatch_branch="subagent",
        purpose="Unavailable test agent.",
        ownership_boundary="No current ownership.",
        supported_task_categories=("host_discovery",),
        excluded_task_categories=(),
        enabled=False,
        max_active_runs_per_task=1,
        requires_resolved_target=True,
    )
    registry = SubagentRegistry((disabled,))

    assert registry.names() == ()
    assert registry.get("disabled_pathfinder") is None
    assert registry.classifier_catalog() == ()


def test_classifier_prompt_renders_registry_metadata_without_agent_hardcoding() -> None:
    registry = get_subagent_registry()

    prompt = build_classifier_system_prompt(
        subagent_catalog=registry.classifier_catalog()
    )

    assert "Registered Subagent Catalog:" in prompt
    assert "- Name: pathfinder" in prompt
    assert "Purpose: Perform bounded network reconnaissance" in prompt
    assert "Supported task categories: host_discovery, port_scanning" in prompt
    assert "Maximum active runs per task: 1" in prompt
