"""Tests for the one-tool DrowAI delivery workflow state machine."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / ".codex/skills/drowai-tool-delivery-workflow"
SCRIPT_PATH = SKILL_ROOT / "scripts/manage_tool_delivery_state.py"
STATE_EXAMPLE = (
    REPO_ROOT / ".codex/agents/drowai-tool-delivery-workflow-state.example.md"
)
FIXTURE_ROOT = REPO_ROOT / "tests/codex_workflows/fixtures/tool_delivery_states"


def _load_manager():
    spec = importlib.util.spec_from_file_location("tool_delivery_state_manager", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(name: str) -> dict[str, object]:
    payload = yaml.safe_load((FIXTURE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_delivery_state_example_parses_and_has_one_tool_guard() -> None:
    manager = _load_manager()
    payload = yaml.safe_load(
        manager.extract_yaml_block(STATE_EXAMPLE.read_text(encoding="utf-8"))
    )

    assert payload["one_tool_only"] is True
    assert payload["next_tool_allowed"] is False
    assert payload["quality"]["max_refactor_rounds"] == 1


def test_normal_transition_path_and_refactor_loop_are_allowed() -> None:
    manager = _load_manager()
    path = [
        "READY",
        "ANALYZING",
        "BRANCH_READY",
        "GUIDE_REVIEW",
        "IMPLEMENTING",
        "IMPLEMENTATION_REVIEW",
        "SECURITY_REVIEW",
        "MECHANICAL_VALIDATION",
        "QUALITY_REVIEW",
        "REFACTORING",
        "SECURITY_REVIEW",
        "MECHANICAL_VALIDATION",
        "QUALITY_REVIEW",
        "FINAL_REVIEW",
        "READY_FOR_PR",
        "PR_OPENED",
        "AWAITING_MERGE",
        "MERGED",
    ]

    assert all(manager.can_transition(current, target) for current, target in zip(path, path[1:]))


def test_next_tool_is_blocked_until_merge_or_recorded_decision() -> None:
    manager = _load_manager()
    awaiting = _fixture("invalid_second_tool_transition.yaml")
    decision = _fixture("not_planned_decision.yaml")

    assert manager.can_start_new_tool(awaiting, open_delivery_prs=1) is False
    assert manager.can_start_new_tool(decision, open_delivery_prs=0) is True
    assert manager.can_transition("AWAITING_MERGE", "READY") is False


def test_stop_status_resumes_only_to_recorded_stage() -> None:
    manager = _load_manager()

    assert manager.can_transition(
        "BLOCKED",
        "SECURITY_REVIEW",
        resume_status="SECURITY_REVIEW",
    )
    assert not manager.can_transition(
        "BLOCKED",
        "IMPLEMENTING",
        resume_status="SECURITY_REVIEW",
    )
    assert not manager.can_transition("NEEDS_CLARIFICATION", "ANALYZING")


def test_valid_pr_state_passes_and_invalid_merge_state_fails() -> None:
    manager = _load_manager()
    valid = _fixture("valid_delivery_state.yaml")
    invalid = _fixture("invalid_second_tool_transition.yaml")

    assert manager.validate_delivery_state(valid) == []
    errors = manager.validate_delivery_state(invalid)
    assert "next_tool_allowed" in errors
    assert "validation_gates.static_security" in errors


def test_security_gate_cannot_pass_without_recorded_report() -> None:
    manager = _load_manager()
    state = _fixture("valid_delivery_state.yaml")
    state["static_security_review"]["report_ref"] = ""  # type: ignore[index]

    errors = manager.validate_delivery_state(state)

    assert "static_security_review.report_ref" in errors


def test_reset_and_cleanup_touch_only_explicit_temp_state(
    tmp_path: Path,
) -> None:
    manager = _load_manager()
    example = tmp_path / ".codex/agents/example-state.example.md"
    live = tmp_path / ".codex/agents/example-state.md"
    example.parent.mkdir(parents=True)
    example.write_text("```yaml\nstatus: READY\n```\n", encoding="utf-8")
    pairs = (manager.StatePair(str(example.relative_to(tmp_path)), str(live.relative_to(tmp_path))),)

    assert manager.reset_child_states(tmp_path, apply=False, state_pairs=pairs)
    assert not live.exists()
    manager.reset_child_states(tmp_path, apply=True, state_pairs=pairs)
    assert yaml.safe_load(live.read_text(encoding="utf-8")) == {"status": "READY"}
    manager.cleanup_child_states(tmp_path, apply=True, state_pairs=pairs)
    assert not live.exists()


def test_final_cleanup_removes_delivery_and_explicit_child_state(
    tmp_path: Path,
) -> None:
    manager = _load_manager()
    agents = tmp_path / ".codex/agents"
    agents.mkdir(parents=True)
    example = agents / "example-state.example.md"
    child = agents / "example-state.md"
    delivery = agents / "drowai-tool-delivery-workflow-state.md"
    unrelated = agents / "cleanup-state.md"
    example.write_text("status: READY\n", encoding="utf-8")
    child.write_text("status: COMPLETE\n", encoding="utf-8")
    delivery.write_text("status: AWAITING_MERGE\n", encoding="utf-8")
    unrelated.write_text("status: RUNNING\n", encoding="utf-8")
    pairs = (
        manager.StatePair(
            str(example.relative_to(tmp_path)),
            str(child.relative_to(tmp_path)),
        ),
    )

    actions = manager.cleanup_workflow_states(
        tmp_path,
        apply=True,
        state_pairs=pairs,
    )

    assert actions == [
        ".codex/agents/example-state.md",
        manager.DELIVERY_LIVE_STATE,
    ]
    assert not child.exists()
    assert not delivery.exists()
    assert unrelated.exists()


def test_apply_requires_explicit_no_active_workflow_confirmation(
    tmp_path: Path,
) -> None:
    manager = _load_manager()

    result = manager.main(
        [
            "cleanup-workflow-states",
            "--repo-root",
            str(tmp_path),
            "--apply",
        ]
    )

    assert result == 1


def test_delivery_skill_is_codex_canonical_and_has_no_todos() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in SKILL_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".md", ".py", ".toml", ".yaml"}
    )

    assert ".cursor" not in content
    assert "[TODO" not in content
    assert "one-tool" in content.lower() or "one tool" in content.lower()
    assert "AWAITING_MERGE" in content
