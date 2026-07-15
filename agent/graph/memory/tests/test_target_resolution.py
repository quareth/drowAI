"""Focused tests for centralized target resolution helpers."""

from __future__ import annotations

from agent.graph.memory.target_resolution import (
    coerce_target_value,
    extract_target_token,
    resolve_active_target_from_working_memory,
    resolve_planner_target,
    resolve_target_from_history,
    resolve_target_from_working_memory,
)


def test_extract_target_token_handles_cidr_ip_and_hostname() -> None:
    assert extract_target_token("scan 10.0.0.0/24 now") == "10.0.0.0/24"
    assert extract_target_token("host 172.17.0.1 is reachable") == "172.17.0.1"
    assert extract_target_token("query api.example.com") == "api.example.com"


def test_coerce_target_value_supports_nested_payloads_and_single_labels() -> None:
    assert coerce_target_value({"target": {"value": "192.168.56.10"}}) == "192.168.56.10"
    assert coerce_target_value(["not-a-target", {"host": "scanme.nmap.org"}]) == "scanme.nmap.org"
    assert coerce_target_value("localhost") == "localhost"
    assert coerce_target_value("scan") is None
    assert coerce_target_value("kali") is None
    assert coerce_target_value("kali", allow_single_label=True) == "kali"
    assert coerce_target_value("/etc/passwd") == "/etc/passwd"
    assert coerce_target_value("/api/v1/users?token=abc") == "/api/v1/users"
    assert coerce_target_value("https://api.example.com/v1/users?token=abc") == "https://api.example.com/v1/users"
    assert coerce_target_value("please scan it") is None


def test_coerce_target_value_uses_explicit_field_specs_for_non_default_keys() -> None:
    payload = {"endpoint": "https://svc.example.com/api?token=secret"}

    assert coerce_target_value(payload) is None
    assert (
        coerce_target_value(
            payload,
            field_specs=(("endpoint", ("url", "url_path"), False),),
        )
        == "https://svc.example.com/api"
    )


def test_resolve_target_from_history_prefers_recent_entries() -> None:
    history = [
        {"role": "assistant", "content": "older target 10.0.0.1"},
        {"role": "assistant", "content": "latest target 10.0.0.2"},
    ]
    assert resolve_target_from_history(history) == "10.0.0.2"


def test_resolve_target_from_working_memory_prefers_active_referent() -> None:
    working_memory = {
        "active": {"target_id": "target:tool:run-1:target"},
        "referents": {
            "intent:target": {"value": "10.0.0.1"},
            "tool:run-1:target": {"value": "10.0.0.2"},
        },
        "recent_turns": [{"role": "user", "content_excerpt": "scan 10.0.0.3"}],
        "input": {"user_message_excerpt": "scan 10.0.0.4"},
    }
    assert resolve_target_from_working_memory(working_memory) == "10.0.0.2"


def test_resolve_target_from_working_memory_falls_back_to_recent_turns() -> None:
    working_memory = {
        "active": {"target_id": None},
        "referents": {},
        "recent_turns": [{"role": "assistant", "content_excerpt": "run scan against 172.16.0.5"}],
    }
    assert resolve_target_from_working_memory(working_memory) == "172.16.0.5"


def test_resolve_active_target_from_working_memory_reads_bound_target_only() -> None:
    working_memory = {
        "active": {"target_id": "target:intent:target"},
        "referents": {"intent:target": {"value": "10.0.0.9"}},
        "recent_turns": [{"role": "user", "content_excerpt": "scan 172.16.0.5"}],
    }
    assert resolve_active_target_from_working_memory(working_memory) == "10.0.0.9"


def test_resolve_active_target_from_working_memory_does_not_fallback_to_recent_turns() -> None:
    working_memory = {
        "active": {"target_id": None},
        "referents": {},
        "recent_turns": [{"role": "user", "content_excerpt": "scan 172.16.0.5"}],
    }
    assert resolve_active_target_from_working_memory(working_memory) is None


def test_resolve_planner_target_uses_continuity_authorized_active_target() -> None:
    metadata = {
        "intent_target_resolution": {"target_status": "unresolved", "resolved_target": None},
        "intent_target_continuity": {"status": "allow", "source": "classifier", "evidence": "follow-up"},
        "working_memory": {
            "active": {"target_id": "target:intent:target"},
            "referents": {"intent:target": {"value": "10.0.0.5"}},
        },
    }
    resolved = resolve_planner_target(
        user_message="scan it then",
        request_targets=[],
        metadata=metadata,
        history=[],
        tool_intent={},
    )
    assert resolved == "10.0.0.5"


def test_resolve_planner_target_blocks_reuse_when_continuity_disallow() -> None:
    metadata = {
        "intent_target_resolution": {"target_status": "unresolved", "resolved_target": None},
        "intent_target_continuity": {"status": "disallow", "source": "classifier", "evidence": None},
        "working_memory": {
            "active": {"target_id": "target:intent:target"},
            "referents": {"intent:target": {"value": "10.0.0.5"}},
        },
    }
    resolved = resolve_planner_target(
        user_message="scan the network",
        request_targets=[],
        metadata=metadata,
        history=[],
        tool_intent={},
    )
    assert resolved == ""
