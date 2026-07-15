"""Focused tests for deterministic working-memory rendering."""

from __future__ import annotations

from agent.graph.memory.render import render_working_memory
from agent.graph.memory.working_memory import create_working_memory


def test_renderer_is_deterministic_and_contains_core_sections() -> None:
    memory = create_working_memory()
    memory["stage"] = "tool_execution"
    memory["objective"]["text"] = "Enumerate host services"
    memory["constraints"]["scope"] = ["lab-only"]
    memory["open_questions"] = [{"code": "target_required", "message": "Need target"}]

    first = render_working_memory(memory, max_chars=1000)
    second = render_working_memory(memory, max_chars=1000)

    assert first == second
    assert "stage: tool_execution" in first
    assert "objective: Enumerate host services" in first
    assert "constraints.scope: lab-only" in first
    assert "last_tool_run:" in first
    assert "coverage_gaps:" in first
    assert "open_questions:" in first


def test_renderer_is_bounded_by_max_chars() -> None:
    memory = create_working_memory()
    memory["objective"]["text"] = "x" * 2000

    rendered = render_working_memory(memory, max_chars=180)
    assert len(rendered) <= 180


def test_renderer_masks_sensitive_values() -> None:
    memory = create_working_memory()
    memory["open_questions"] = [
        {"code": "missing_secret", "message": "Need api_key token for service access"}
    ]
    memory["preferences"]["language"] = "en"

    rendered = render_working_memory(memory, max_chars=1000)
    assert "api_key" not in rendered
    assert "token" not in rendered
    assert "<REDACTED>" in rendered


def test_renderer_uses_latest_tool_run_summary() -> None:
    memory = create_working_memory()
    memory["tool_runs"] = [
        {"tool_id": "nmap_scan", "summary": "first summary"},
        {"tool_id": "http_probe", "summary": "latest summary"},
    ]
    rendered = render_working_memory(memory, max_chars=1000)
    assert "last_tool_run: http_probe: latest summary" in rendered


def test_renderer_includes_active_target_from_referent() -> None:
    memory = create_working_memory()
    memory["referents"]["intent:target"] = {"value": "172.17.0.1"}
    memory["active"]["target_id"] = "target:intent:target"

    rendered = render_working_memory(memory, max_chars=1000)
    assert "active_target: 172.17.0.1" in rendered


def test_renderer_suppresses_target_gap_noise_in_tool_selection_stage() -> None:
    memory = create_working_memory()
    memory["stage"] = "tool_selection"
    memory["open_questions"] = [
        {
            "code": "target_handle_required",
            "message": "A target handle is required before continuing tool-path execution.",
        }
    ]

    rendered = render_working_memory(memory, max_chars=1000)
    assert "target_handle_required" not in rendered
    assert "A target handle is required" not in rendered
