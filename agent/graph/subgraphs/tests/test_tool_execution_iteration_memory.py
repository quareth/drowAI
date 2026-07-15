"""Tests for tool-execution iteration-memory ledger append (Phase 2).

Purpose
-------
Validates that tool execution appends one deterministic PTR section-snapshot
tool phase record to the shared current-turn phase ledger
(``metadata["working_memory"]["current_turn_phases"]``) after final compact
metadata exists. The record is derived from the same last-tool projection PTR
uses for current tool output - no prose parsing is performed.

Scope
-----
- Tool projection appends one section-snapshot ledger record with
  ``source="tool"`` when ``turn_sequence`` is present.
- Identity fields are runtime-stamped by the helper; the projection never
  supplies ``turn_sequence``/``phase_sequence``/``source`` inline.
- When ``turn_sequence`` is missing or non-int, no ledger append happens
  (silent DEBUG-level degradation) and the rest of the projection is
  unaffected.
- ``metadata["tool_execution_history"]`` shape remains unchanged.

Interleaving of PTR + tool ledger records in one turn is covered in
``agent/graph/nodes/post_tool_reasoning/tests/test_iteration_memory_continuity.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional
from unittest.mock import AsyncMock

import pytest

import agent.graph.nodes  # noqa: F401  # Prime node package to avoid import-cycle test collection.

from agent.graph.subgraphs.tool_execution_runtime.result_state_projection import (
    apply_result_state_projection,
    append_tool_phase_snapshot_from_metadata,
    compact_observation_text,
    project_result_state,
)
from agent.graph.utils.iteration_memory import (
    append as iteration_memory_append,
    get_current_turn_scope,
    get_ledger,
    render_phase_memory_section,
)

_LEGACY_PHASE_FIELDS = {
    "kind",
    "target",
    "hypothesis",
    "action",
    "status",
    "result",
    "failure_category",
    "summary",
    "terminal_for_hypothesis",
}


# ---------------------------------------------------------------------------
# Lightweight stand-ins for the dependency-injected projection boundary.
# ---------------------------------------------------------------------------


class _StubCompression:
    source = "llm"
    fallback_reason = None


class _StubCompactResult:
    """Minimal stand-in for the compact-compression envelope."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.compression = _StubCompression()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._payload)


class _StubCompressionResult:
    """Minimal stand-in for the compressor result contract."""

    def __init__(
        self,
        compact_output: _StubCompactResult,
        usage_record: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.compact_output = compact_output
        self.usage_record = usage_record


@dataclass
class _StubOutcome:
    """Minimal stand-in for ``ToolExecutionOutcome`` at projection time."""

    tool_id: Optional[str] = "nmap"
    parameters: Dict[str, Any] = field(
        default_factory=lambda: {"target": "10.0.0.1", "ports": "21"}
    )
    result: Dict[str, Any] = field(
        default_factory=lambda: {
            "tool": "nmap",
            "success": True,
            "status": "success",
            "stdout": "21/tcp filtered",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.1,
        }
    )
    summary: str = "21/tcp filtered"
    reasoning: List[str] = field(default_factory=list)
    duration: float = 0.1
    catalog: List[Any] = field(default_factory=list)

    def to_graph_metadata(self) -> Dict[str, Any]:
        return {
            "tool": self.tool_id,
            "parameters": dict(self.parameters),
            "reasoning": list(self.reasoning),
            "catalog": [],
            "result": dict(self.result),
            "duration": self.duration,
            "summary": self.summary,
        }


@dataclass
class _StubTrace:
    observations: List[str] = field(default_factory=list)
    scratchpad: Optional[str] = None
    usage_records: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class _StubFacts:
    metadata: Dict[str, Any] = field(default_factory=dict)
    iterations: int = 1
    selected_tool: str = "nmap"
    tool_parameters: Dict[str, Any] = field(
        default_factory=lambda: {"target": "10.0.0.1", "ports": "21"}
    )


@dataclass
class _StubInteractive:
    trace: _StubTrace = field(default_factory=_StubTrace)


def _working_memory(metadata: Dict[str, Any]) -> Dict[str, Any]:
    raw = metadata.get("working_memory")
    if isinstance(raw, dict):
        return raw
    return {}


def _ledger_ref(metadata: Dict[str, Any]) -> list[Any]:
    raw = _working_memory(metadata).get("current_turn_phases")
    if isinstance(raw, list):
        return raw
    return []


def _counter(metadata: Dict[str, Any]) -> int | None:
    value = _working_memory(metadata).get("current_turn_phase_counter")
    return value if isinstance(value, int) else None


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _identity_bundle(previous: Any = None, **_kwargs: Any) -> Dict[str, Any]:
    """Stand-in for ``MemoryManager.reduce_tool_result``: keep existing or init."""
    if isinstance(previous, Mapping):
        return dict(previous)
    return {
        "tool_state": {},
        "tool_runs": [],
        "collections": [],
        "active": {},
    }


def _run_projection(
    *,
    facts: _StubFacts,
    outcome: _StubOutcome,
    compact_payload: Optional[Dict[str, Any]] = None,
    compression_usage_record: Optional[Dict[str, Any]] = None,
    interactive: Optional[_StubInteractive] = None,
    tool_name: str = "nmap",
    memory_reduce_tool_result_fn: Any = _identity_bundle,
    append_tool_snapshot: bool = True,
) -> Dict[str, Any]:
    """Drive ``project_result_state`` with pure-Python fakes.

    Returns the projection payload; ``facts.metadata`` is mutated in place
    and is where the ledger lives.
    """
    import asyncio

    compact_payload = compact_payload or {
        "schema_version": "2.0",
        "tool": tool_name,
        "status": "success",
        "success": True,
        "exit_code": 0,
        "summary": outcome.summary,
        "key_findings": [],
        "errors": [],
        "report_recommendations": [],
        "structured_signals": [],
        "decision_evidence": [],
        "lossiness_risk": "low",
        "artifact_refs": [],
        "compression": {"source": "llm", "fallback_reason": None},
    }

    interactive = interactive or _StubInteractive()
    compress_mock = AsyncMock(
        return_value=_StubCompressionResult(
            _StubCompactResult(compact_payload),
            usage_record=compression_usage_record,
        )
    )

    coro = project_result_state(
        interactive=interactive,
        facts=facts,
        outcome=outcome,
        tool_name=tool_name,
        metadata=facts.metadata,
        runtime_context=None,
        artifact_path=None,
        execution_id=None,
        tool_call_id="tc-1",
        turn_sequence=facts.metadata.get("turn_sequence"),
        persisted_artifact_refs=(),
        compact_sanitized_result_keys=(
            "tool",
            "success",
            "status",
            "exit_code",
            "parameters",
        ),
        compact_observation_text_fn=compact_observation_text,
        enrich_artifact_refs_with_provenance_fn=lambda **_kwargs: [],
        refresh_trace_scratchpad_fn=_noop,
        resolve_llm_client_fn=lambda *_a, **_kw: None,
        compress_tool_output_fn=compress_mock,
        compact_output_size_bytes_fn=lambda _envelope: 128,
        record_compression_observability_metrics_fn=_noop,
        memory_reduce_tool_result_fn=memory_reduce_tool_result_fn,
        logger=_StubLogger(),
        safe_inc_fn=_noop,
        safe_gauge_fn=_noop,
    )

    projection = asyncio.run(coro)
    if append_tool_snapshot:
        facts.metadata["last_tool_result_compact"] = dict(
            projection.get("compact_result_dict") or {}
        )
        append_tool_phase_snapshot_from_metadata(
            facts=facts,
            turn_sequence=facts.metadata.get("turn_sequence"),
            logger=_StubLogger(),
        )
    return projection


def _build_projection(
    *,
    facts: _StubFacts,
    outcome: _StubOutcome,
    compression_usage_record: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], _StubInteractive]:
    """Build a projection without applying it to shared graph state."""
    import asyncio

    interactive = _StubInteractive()
    compact_payload = {
        "schema_version": "2.0",
        "tool": "nmap",
        "status": "success",
        "success": True,
        "exit_code": 0,
        "summary": outcome.summary,
        "key_findings": [],
        "errors": [],
        "report_recommendations": [],
        "structured_signals": [],
        "decision_evidence": [],
        "lossiness_risk": "low",
        "artifact_refs": [],
        "compression": {"source": "llm", "fallback_reason": None},
    }
    compress_mock = AsyncMock(
        return_value=_StubCompressionResult(
            _StubCompactResult(compact_payload),
            usage_record=compression_usage_record,
        )
    )

    projection = asyncio.run(
        project_result_state(
            interactive=interactive,
            facts=facts,
            outcome=outcome,
            tool_name="nmap",
            metadata=facts.metadata,
            runtime_context=None,
            artifact_path=None,
            execution_id=None,
            tool_call_id="tc-1",
            turn_sequence=facts.metadata.get("turn_sequence"),
            persisted_artifact_refs=(),
            compact_sanitized_result_keys=(
                "tool",
                "success",
                "status",
                "exit_code",
                "parameters",
            ),
            compact_observation_text_fn=compact_observation_text,
            enrich_artifact_refs_with_provenance_fn=lambda **_kwargs: [],
            refresh_trace_scratchpad_fn=_noop,
            resolve_llm_client_fn=lambda *_a, **_kw: None,
            compress_tool_output_fn=compress_mock,
            compact_output_size_bytes_fn=lambda _envelope: 128,
            record_compression_observability_metrics_fn=_noop,
            memory_reduce_tool_result_fn=_identity_bundle,
            logger=_StubLogger(),
            safe_inc_fn=_noop,
            safe_gauge_fn=_noop,
            apply_to_state=False,
        )
    )
    return projection, interactive


class _StubLogger:
    """Absorbs log calls silently so we do not leak into stderr in tests."""

    def debug(self, *_a: Any, **_k: Any) -> None: ...
    def info(self, *_a: Any, **_k: Any) -> None: ...
    def warning(self, *_a: Any, **_k: Any) -> None: ...
    def error(self, *_a: Any, **_k: Any) -> None: ...


# ---------------------------------------------------------------------------
# Compressor usage projection: successful LLM compression only
# ---------------------------------------------------------------------------


class TestCompressorUsageProjection:
    """Tool projection appends compressor usage through trace.usage_records."""

    def test_appends_exactly_one_successful_llm_compressor_usage_record(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 12})
        interactive = _StubInteractive()
        usage_record = {
            "prompt_tokens": 4065,
            "completion_tokens": 465,
            "total_tokens": 4530,
            "model": "claude-haiku-4-5-20251001",
            "provider": "anthropic",
            "api_surface": "messages",
            "cache_reporting": "unknown",
            "request_mode": "non_streaming",
            "provider_usage_components": {
                "provider": "anthropic",
                "api_surface": "messages",
                "components": {"input_tokens": 4065, "output_tokens": 465},
            },
            "source": "tool_output_compressor",
        }

        _run_projection(
            facts=facts,
            outcome=_StubOutcome(),
            interactive=interactive,
            compression_usage_record=usage_record,
        )

        assert interactive.trace.usage_records == [usage_record]

    def test_compact_token_usage_metadata_does_not_create_usage_row(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 12})
        interactive = _StubInteractive()

        _run_projection(
            facts=facts,
            outcome=_StubOutcome(),
            interactive=interactive,
            compact_payload={
                "schema_version": "2.0",
                "tool": "nmap",
                "status": "success",
                "success": True,
                "exit_code": 0,
                "summary": "21/tcp filtered",
                "key_findings": [],
                "errors": [],
                "report_recommendations": [],
                "structured_signals": [],
                "decision_evidence": [],
                "lossiness_risk": "low",
                "artifact_refs": [],
                "compression": {
                    "source": "deterministic",
                    "fallback_reason": "llm_threshold_bypass",
                    "token_usage": {"prompt_tokens": 10, "completion_tokens": 2},
                },
            },
            compression_usage_record=None,
        )

        assert interactive.trace.usage_records == []

    def test_deferred_projection_appends_usage_to_shared_state_once(self) -> None:
        call_facts = _StubFacts(metadata={"turn_sequence": 12})
        usage_record = {
            "prompt_tokens": 4065,
            "completion_tokens": 465,
            "total_tokens": 4530,
            "model": "claude-haiku-4-5-20251001",
            "provider": "anthropic",
            "source": "tool_output_compressor",
            "request_mode": "non_streaming",
        }

        projection, call_interactive = _build_projection(
            facts=call_facts,
            outcome=_StubOutcome(),
            compression_usage_record=usage_record,
        )
        shared_interactive = _StubInteractive()
        shared_facts = _StubFacts(metadata={"turn_sequence": 12})

        assert call_interactive.trace.usage_records == []

        apply_result_state_projection(
            interactive=shared_interactive,
            facts=shared_facts,
            outcome=_StubOutcome(),
            projection=projection,
            execution_id=None,
            tool_call_id="tc-1",
            turn_sequence=12,
            compact_observation_text_fn=compact_observation_text,
            refresh_trace_scratchpad_fn=_noop,
            memory_reduce_tool_result_fn=_identity_bundle,
            logger=_StubLogger(),
            safe_inc_fn=_noop,
        )

        assert shared_interactive.trace.usage_records == [usage_record]


# ---------------------------------------------------------------------------
# Ledger append: happy path with structured runtime data
# ---------------------------------------------------------------------------


class TestToolLedgerAppend:
    """Tool projection appends one ordered tool phase record."""

    def test_observed_findings_use_endpoint_parameter_target_hint(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 12})
        captured: dict[str, Any] = {}
        outcome = _StubOutcome(
            parameters={"command": "echo hello", "endpoint": "http://10.0.0.1/status"},
            result={
                "tool": "nmap",
                "success": True,
                "status": "success",
                "stdout": "80/tcp open http",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
                "metadata": {
                    "host_status": "up",
                    "open_ports": [
                        {
                            "port": 80,
                            "protocol": "tcp",
                            "status": "open",
                            "service": "http",
                        }
                    ],
                },
            },
            summary="80/tcp open http",
        )

        def _capture_bundle(previous: Any = None, **kwargs: Any) -> Dict[str, Any]:
            captured["observed_findings"] = kwargs.get("observed_findings")
            return _identity_bundle(previous=previous, **kwargs)

        _run_projection(
            facts=facts,
            outcome=outcome,
            memory_reduce_tool_result_fn=_capture_bundle,
        )

        observed_findings = captured["observed_findings"]
        assert any(item["kind"] == "host_up" and item["target"] == "10.0.0.1" for item in observed_findings)
        assert any(
            item["kind"] == "port_open" and item["subject"] == "10.0.0.1:80/tcp"
            for item in observed_findings
        )

    def test_appends_single_tool_record_with_source_tool(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 12})

        _run_projection(facts=facts, outcome=_StubOutcome())

        ledger = get_ledger(facts.metadata)
        assert isinstance(ledger, list)
        assert len(ledger) == 1

        record = ledger[0]
        assert record["source"] == "tool"
        assert record["sections"] == [
            {
                "heading": "Tool Executed",
                "body": "Tool: nmap\nParameters: target=10.0.0.1, ports=21",
            },
            {"heading": "Tool Output Summary", "body": "21/tcp filtered"},
            {"heading": "Compression Lossiness", "body": "lossiness_risk: low"},
        ]
        assert _LEGACY_PHASE_FIELDS.isdisjoint(record)

    def test_projection_does_not_append_before_final_compact_metadata(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 12})

        _run_projection(
            facts=facts,
            outcome=_StubOutcome(),
            append_tool_snapshot=False,
        )

        assert get_ledger(facts.metadata) == []

    def test_final_snapshot_renders_metasploit_params_no_session_and_batch_evidence(
        self,
    ) -> None:
        tool_id = "exploitation_tools.metasploit.run_exploit"
        no_session_summary = "No active Metasploit session was created for this target."
        facts = _StubFacts(
            metadata={"turn_sequence": 13},
            selected_tool=tool_id,
            tool_parameters={
                "module": "exploit/unix/ftp/vsftpd_234_backdoor",
                "RHOSTS": "10.0.0.5",
                "RPORT": 21,
            },
        )
        compact_payload = {
            "schema_version": "2.0",
            "tool": tool_id,
            "status": "success",
            "success": True,
            "exit_code": 0,
            "summary": no_session_summary,
            "key_findings": ["vsftpd exploit did not open a session"],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": ["session_count=0"],
            "lossiness_risk": "low",
            "artifact_refs": [],
            "compression": {"source": "llm", "fallback_reason": None},
        }
        outcome = _StubOutcome(
            tool_id=tool_id,
            parameters={
                "module": "exploit/unix/ftp/vsftpd_234_backdoor",
                "RHOSTS": "10.0.0.5",
                "RPORT": 21,
            },
            result={
                "tool": tool_id,
                "success": True,
                "status": "success",
                "stdout": "Exploit completed, but no session was created.",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
            },
            summary=no_session_summary,
        )

        projection = _run_projection(
            facts=facts,
            outcome=outcome,
            tool_name=tool_id,
            compact_payload=compact_payload,
            append_tool_snapshot=False,
        )
        facts.metadata["last_tool_result_compact"] = dict(
            projection["compact_result_dict"]
        )
        facts.metadata["last_tool_result_compact_batch"] = {
            "tool_batch_id": "tb_msf",
            "execution_strategy": "sequential",
            "status": "completed",
            "success": True,
            "results": [
                {
                    "tool_call_id": "tc_msf",
                    "tool_id": tool_id,
                    "intent": "attempt exploit",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": dict(projection["compact_result_dict"]),
                }
            ],
        }

        append_tool_phase_snapshot_from_metadata(
            facts=facts,
            turn_sequence=13,
            logger=_StubLogger(),
        )

        record = get_ledger(facts.metadata)[0]
        headings = [section["heading"] for section in record["sections"]]
        assert headings == [
            "Tool Executed",
            "Tool Output Summary",
            "Batch Tool Results",
            "Key Findings",
            "Decision Evidence",
            "Compression Lossiness",
        ]
        phase_render = render_phase_memory_section(facts.metadata, turn_sequence=13)
        assert "## Tool Executed" in phase_render
        assert "Tool: exploitation_tools.metasploit.run_exploit" in phase_render
        assert "module=exploit/unix/ftp/vsftpd_234_backdoor" in phase_render
        assert "RHOSTS=10.0.0.5" in phase_render
        assert "RPORT=21" in phase_render
        assert no_session_summary in phase_render
        assert "## Batch Tool Results" in phase_render
        assert "summary=No active Metasploit session" in phase_render
        assert "## Decision Evidence" in phase_render
        assert "session_count=0" in phase_render
        assert _LEGACY_PHASE_FIELDS.isdisjoint(record)

    def test_identity_fields_are_runtime_stamped(self) -> None:
        """turn_sequence/phase_sequence must come from the helper, not the projection."""
        facts = _StubFacts(metadata={"turn_sequence": 7})

        _run_projection(facts=facts, outcome=_StubOutcome())

        metadata = facts.metadata
        record = get_ledger(metadata)[0]

        assert record["turn_sequence"] == 7
        # First record of this turn gets phase_sequence 0.
        assert record["phase_sequence"] == 0
        # Helper advanced the per-turn counter after reserving.
        assert _counter(metadata) == 1
        assert get_current_turn_scope(metadata) == 7

    def test_failed_tool_snapshot_records_prompt_summary_only(self) -> None:
        """Failure details stay in the prompt-facing summary, not legacy fields."""
        # Failure outcome: no structured "status" key, success=False.
        outcome = _StubOutcome(
            result={
                "tool": "nmap",
                "success": False,
                "stdout": "",
                "stderr": "connection refused",
                "exit_code": 1,
                "duration": 0.1,
            },
            summary="Connection refused",
        )
        facts = _StubFacts(metadata={"turn_sequence": 9})

        _run_projection(
            facts=facts,
            outcome=outcome,
            compact_payload={
                "schema_version": "2.0",
                "tool": "nmap",
                "status": "error",
                "success": False,
                "exit_code": 1,
                "summary": "Connection refused",
                "key_findings": [],
                "errors": ["connection refused"],
                "report_recommendations": [],
                "structured_signals": [],
                "decision_evidence": [],
                "lossiness_risk": "medium",
                "artifact_refs": [],
                "compression": {"source": "llm", "fallback_reason": None},
            },
        )

        record = get_ledger(facts.metadata)[0]
        assert record["sections"][1] == {
            "heading": "Tool Output Summary",
            "body": "Connection refused",
        }
        assert _LEGACY_PHASE_FIELDS.isdisjoint(record)

    def test_uses_compact_summary_when_status_field_present(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 3})
        outcome = _StubOutcome(
            result={
                "tool": "nmap",
                "success": True,
                "status": "success",
                "stdout": "ok",
                "stderr": "",
                "exit_code": 0,
                "duration": 0.1,
            },
        )

        _run_projection(facts=facts, outcome=outcome)

        record = get_ledger(facts.metadata)[0]
        assert record["sections"][1] == {
            "heading": "Tool Output Summary",
            "body": "21/tcp filtered",
        }
        assert _LEGACY_PHASE_FIELDS.isdisjoint(record)

    def test_records_runtime_unavailable_tool_control_signal(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 11})
        outcome = _StubOutcome(
            tool_id="information_gathering.network_discovery.netdiscover",
            result={
                "tool": "information_gathering.network_discovery.netdiscover",
                "success": False,
                "status": "error",
                "stdout": "",
                "stderr": "bash: netdiscover: command not found",
                "exit_code": 127,
                "duration": 0.1,
            },
            summary="bash: netdiscover: command not found",
        )

        _run_projection(
            facts=facts,
            outcome=outcome,
            tool_name="information_gathering.network_discovery.netdiscover",
            compact_payload={
                "schema_version": "2.0",
                "tool": "information_gathering.network_discovery.netdiscover",
                "status": "error",
                "success": False,
                "exit_code": 127,
                "summary": "bash: netdiscover: command not found",
                "key_findings": [],
                "errors": ["bash: netdiscover: command not found"],
                "report_recommendations": [],
                "structured_signals": [],
                "decision_evidence": [],
                "lossiness_risk": "medium",
                "artifact_refs": [],
                "compression": {"source": "llm", "fallback_reason": None},
            },
        )

        assert facts.metadata["current_turn_runtime_controls"] == {
            "turn_sequence": 11,
            "unavailable_tools": [
                "information_gathering.network_discovery.netdiscover"
            ],
        }

    def test_section_snapshot_omits_legacy_target_field(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 4})
        outcome = _StubOutcome(parameters={"command": "echo hello"})

        _run_projection(facts=facts, outcome=outcome)

        record = get_ledger(facts.metadata)[0]
        assert "target" not in record
        assert record["sections"][0] == {
            "heading": "Tool Executed",
            "body": "Tool: nmap\nParameters: command=echo hello",
        }

    def test_target_list_parameter_renders_as_compact_string(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 5})
        outcome = _StubOutcome(
            parameters={"target": ["10.0.0.1", "10.0.0.2"]}
        )

        _run_projection(facts=facts, outcome=outcome)

        record = get_ledger(facts.metadata)[0]
        assert record["sections"][0] == {
            "heading": "Tool Executed",
            "body": "Tool: nmap\nParameters: target=['10.0.0.1', '10.0.0.2']",
        }

    def test_legacy_hypothesis_and_terminal_fields_remain_unset(self) -> None:
        """Projection must not invent legacy semantic fields."""
        facts = _StubFacts(metadata={"turn_sequence": 6})

        _run_projection(facts=facts, outcome=_StubOutcome())

        record = get_ledger(facts.metadata)[0]
        assert "hypothesis" not in record
        assert "terminal_for_hypothesis" not in record
        assert _LEGACY_PHASE_FIELDS.isdisjoint(record)

    def test_timeout_failure_maps_to_timeout_result(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 10})
        outcome = _StubOutcome(
            result={
                "tool": "http_request",
                "success": False,
                "status": "error",
                "stdout": "",
                "stderr": "",
                "exit_code": 124,
                "duration": 5.0,
            },
            summary="HTTP request timed out",
        )

        _run_projection(
            facts=facts,
            outcome=outcome,
            tool_name="http_request",
            compact_payload={
                "schema_version": "2.0",
                "tool": "http_request",
                "status": "error",
                "success": False,
                "exit_code": 124,
                "summary": "HTTP request timed out",
                "key_findings": [],
                "errors": ["operation timeout"],
                "report_recommendations": [],
                "structured_signals": [],
                "decision_evidence": [],
                "lossiness_risk": "medium",
                "artifact_refs": [],
                "compression": {"source": "llm", "fallback_reason": None},
            },
        )

        record = get_ledger(facts.metadata)[0]
        assert record["sections"][1] == {
            "heading": "Tool Output Summary",
            "body": "HTTP request timed out",
        }
        assert _LEGACY_PHASE_FIELDS.isdisjoint(record)


# ---------------------------------------------------------------------------
# Degradation contract: missing / non-int turn_sequence
# ---------------------------------------------------------------------------


class TestToolLedgerAppendNoop:
    """When runtime identity is absent the append is a silent no-op."""

    def test_no_append_when_turn_sequence_missing(self) -> None:
        facts = _StubFacts(metadata={})  # no turn_sequence

        _run_projection(facts=facts, outcome=_StubOutcome())

        # Ledger never initialized; projection still completed cleanly.
        assert get_ledger(facts.metadata) == []
        # Compatibility surfaces unaffected.
        assert "tool_execution_history" in facts.metadata
        assert isinstance(facts.metadata["tool_execution_history"], list)

    def test_no_append_when_turn_sequence_is_string(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": "not-an-int"})

        _run_projection(facts=facts, outcome=_StubOutcome())

        assert get_ledger(facts.metadata) == []
        assert "tool_execution_history" in facts.metadata

    def test_no_append_when_turn_sequence_is_none(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": None})

        _run_projection(facts=facts, outcome=_StubOutcome())

        assert get_ledger(facts.metadata) == []


# ---------------------------------------------------------------------------
# tool_execution_history must remain untouched in shape and semantics
# ---------------------------------------------------------------------------


class TestToolExecutionHistoryUnchanged:
    """The scan-optimization record shape is not changed by the ledger dual-write."""

    def test_tool_execution_history_keys_are_stable(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 1})

        _run_projection(facts=facts, outcome=_StubOutcome())

        history = facts.metadata["tool_execution_history"]
        assert isinstance(history, list)
        assert len(history) == 1
        record = history[0]
        # Keys match ``ToolExecution`` dataclass serialization exactly.
        assert set(record.keys()) == {
            "tool_id",
            "parameters",
            "parameter_hash",
            "result_summary",
            "timestamp",
            "iteration",
        }
        assert record["tool_id"] == "nmap"
        assert record["result_summary"] == "21/tcp filtered"

    def test_tool_execution_history_and_ledger_are_independent_lists(self) -> None:
        facts = _StubFacts(metadata={"turn_sequence": 1})

        _run_projection(facts=facts, outcome=_StubOutcome())

        history = facts.metadata["tool_execution_history"]
        ledger = _ledger_ref(facts.metadata)
        # Sanity: separate lists, separate record shapes.
        assert history is not ledger
        assert "phase_sequence" not in history[0]
        assert "parameter_hash" not in ledger[0]

    def test_action_history_records_include_turn_sequence(self) -> None:
        """Projection annotates action history with the active turn for chain-aware PTR."""
        facts = _StubFacts(metadata={"turn_sequence": 21})

        _run_projection(facts=facts, outcome=_StubOutcome())

        action_history = facts.metadata["action_history"]
        assert isinstance(action_history, list)
        assert len(action_history) == 1
        assert action_history[0] == {
            "tool_id": "nmap",
            "params": {"target": "10.0.0.1", "ports": "21"},
            "turn_sequence": 21,
        }


# ---------------------------------------------------------------------------
# Interleaving ordering (PTR + tool) in one turn
# ---------------------------------------------------------------------------


class TestPtrAndToolInterleaveOrdering:
    """PTR and tool appends share one monotonic phase counter per turn."""

    def test_ptr_tool_ptr_ordering_is_deterministic(self) -> None:
        metadata: Dict[str, Any] = {"turn_sequence": 12}

        # Simulate a PTR append from the recorder (Task 3.1) ...
        iteration_memory_append(
            metadata,
            turn_sequence=12,
            source="ptr",
            payload={
                "sections": [
                    {"heading": "PTR Decision", "body": "PTR phase one"},
                ],
            },
        )
        # ... then drive a tool projection (Task 3.2) ...
        facts = _StubFacts(metadata=metadata)
        _run_projection(facts=facts, outcome=_StubOutcome())
        # ... then another PTR append.
        iteration_memory_append(
            metadata,
            turn_sequence=12,
            source="ptr",
            payload={
                "sections": [
                    {"heading": "PTR Decision", "body": "PTR phase three"},
                ],
            },
        )

        ledger = _ledger_ref(metadata)
        assert [r["phase_sequence"] for r in ledger] == [0, 1, 2]
        assert [r["source"] for r in ledger] == ["ptr", "tool", "ptr"]
        # Turn identity is shared across the three records.
        assert {r["turn_sequence"] for r in ledger} == {12}


if __name__ == "__main__":  # pragma: no cover - manual run convenience
    pytest.main([__file__, "-v"])
