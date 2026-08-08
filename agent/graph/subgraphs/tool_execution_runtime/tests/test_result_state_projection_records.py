"""Unit tests for compact tool-execution record persistence helpers.

These tests lock the tooling_plane per-call execution record requirement that
per-call execution records persist route/runtime metadata and keep runner
artifacts marked as unpromoted runner-local references until data_plane promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping

from agent.execution_strategy import ExecutionStrategy
from agent.graph.subgraphs.tool_execution_runtime.batch_runner import (
    write_compact_batch_metadata,
)
from agent.graph.subgraphs.tool_execution_runtime.batch_result_application import (
    _enqueue_completed_execution_ingestions,
    _restore_primary_call_metadata_fields,
)
from agent.graph.subgraphs.tool_execution_runtime.approval_and_idempotency import (
    apply_cached_dispatch_result,
    maybe_return_cached_dispatch_update,
    store_dispatch_cache_result,
)
from agent.graph.subgraphs.tool_execution_runtime.result_state_projection import (
    _append_tool_execution_record,
    _sanitize_artifact_refs_for_memory,
    apply_result_state_projection,
    preserve_shell_session_result_fields,
    project_trace_history_and_outbound_events,
    sanitize_tool_result_for_metadata,
)
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)
from core.prompts.builders.post_tool.evidence import read_compact_evidence
from core.prompts.builders.post_tool.last_tool import extract_last_tool_sections
from agent.tool_runtime.output_persistence_policy import resolve_output_persistence


@dataclass
class _Facts:
    metadata: dict[str, Any]
    iterations: int = 1
    task_id: int = 1


@dataclass
class _Outcome:
    result: Mapping[str, Any]
    duration: float


def test_completed_batch_enqueues_each_persisted_execution_and_isolates_failures() -> None:
    """Every persisted call should enqueue independently from sibling failures."""
    facts = _Facts(metadata={}, task_id=42)
    batch = ToolBatch(
        tool_batch_id="tb-ingestion",
        tool_calls=(
            ToolCall("tc-1", "nmap", {"target": "127.0.0.1"}),
            ToolCall("tc-2", "curl", {"url": "https://example.test"}),
            ToolCall("tc-3", "shell.exec", {"command": "true"}),
            ToolCall("tc-4", "shell.utility", {"command": "nmap localhost"}),
            ToolCall("tc-5", "shell.assessment", {"command": "ls -la"}),
            ToolCall("tc-6", "shell.write_stdin", {"session_id": "utility-session"}),
            ToolCall("tc-7", "shell.write_stdin", {"session_id": "assessment-session"}),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    attempts: list[dict[str, Any]] = []
    metrics: list[str] = []

    def _enqueue(**kwargs: Any) -> None:
        attempts.append(kwargs)
        if kwargs["execution_id"] == "execution-1":
            raise RuntimeError("first enqueue failed")

    _enqueue_completed_execution_ingestions(
        facts=facts,
        batch=batch,
        execution_id_by_call_id={
            "tc-1": "execution-1",
            "tc-2": "execution-2",
            "tc-3": None,
            "tc-4": "execution-4",
            "tc-5": "execution-5",
            "tc-6": "execution-6",
            "tc-7": "execution-7",
        },
        compact_by_call_id={
            "tc-1": {"summary": "fallback"},
            "tc-2": {"summary": "curl complete"},
            "tc-4": {"summary": "utility transient"},
            "tc-5": {"summary": "assessment retained"},
            "tc-6": {"summary": "utility continuation transient"},
            "tc-7": {"summary": "assessment continuation retained"},
        },
        deterministic_compact_by_call_id={
            "tc-1": {"summary": "nmap complete"},
        },
        outcome_by_call_id={
            "tc-6": _Outcome(
                result={
                    "metadata": {
                        "runtime_session": {"originating_capability": "utility"}
                    }
                },
                duration=0.1,
            ),
            "tc-7": _Outcome(
                result={
                    "metadata": {
                        "runtime_session": {"originating_capability": "assessment"}
                    }
                },
                duration=0.1,
            ),
        },
        deps={
            "_enqueue_execution_ingestion": _enqueue,
            "logger": SimpleNamespace(warning=lambda *_args, **_kwargs: None),
            "safe_inc": metrics.append,
        },
    )

    assert [attempt["execution_id"] for attempt in attempts] == [
        "execution-1",
        "execution-2",
        "execution-5",
        "execution-7",
    ]
    assert attempts[0]["compact_output"]["summary"] == "nmap complete"
    assert attempts[1]["compact_output"]["summary"] == "curl complete"
    assert attempts[2]["compact_output"]["summary"] == "assessment retained"
    assert attempts[3]["compact_output"]["summary"] == (
        "assessment continuation retained"
    )
    assert metrics == ["knowledge_ingestion_enqueue_failures"]


def test_utility_only_batch_clears_output_bearing_primary_metadata() -> None:
    facts = _Facts(
        metadata={
            "selected_tool": "shell.utility",
            "tool_parameters": {"command": "printf transient"},
            "last_tool_result": {"stdout": "transient"},
            "last_artifact_path": "artifacts/transient.txt",
        }
    )
    facts.selected_tool = "shell.utility"
    facts.tool_parameters = {"command": "printf transient"}
    batch = ToolBatch(
        tool_batch_id="tb-utility",
        tool_calls=(
            ToolCall("tc-utility", "shell.utility", {"command": "printf transient"}),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )

    _restore_primary_call_metadata_fields(
        facts=facts,
        batch=batch,
        projection_by_call_id={},
        tool_catalog_by_call_id={},
        cached_dispatch_by_call_id={},
        metadata_patch_by_call_id={},
        persistence_decision_by_call_id={
            "tc-utility": resolve_output_persistence("shell.utility")
        },
    )

    assert facts.selected_tool is None
    assert facts.tool_parameters == {}
    assert set(facts.metadata).isdisjoint(
        {"selected_tool", "tool_parameters", "last_tool_result", "last_artifact_path"}
    )


def test_mixed_batch_restores_first_durable_call_not_utility_call() -> None:
    facts = _Facts(metadata={})
    facts.selected_tool = None
    facts.tool_parameters = {}
    batch = ToolBatch(
        tool_batch_id="tb-mixed",
        tool_calls=(
            ToolCall("tc-utility", "shell.utility", {"command": "pwd"}),
            ToolCall("tc-assessment", "shell.assessment", {"command": "nmap localhost"}),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )

    _restore_primary_call_metadata_fields(
        facts=facts,
        batch=batch,
        projection_by_call_id={
            "tc-assessment": {
                "result_for_metadata": {
                    "tool": "shell.assessment",
                    "summary": "assessment retained",
                }
            }
        },
        tool_catalog_by_call_id={},
        cached_dispatch_by_call_id={},
        metadata_patch_by_call_id={},
        persistence_decision_by_call_id={
            "tc-utility": resolve_output_persistence("shell.utility"),
            "tc-assessment": resolve_output_persistence("shell.assessment"),
        },
    )

    assert facts.selected_tool == "shell.assessment"
    assert facts.tool_parameters == {"command": "nmap localhost"}
    assert facts.metadata["last_tool_result"]["summary"] == "assessment retained"


def test_append_tool_execution_record_persists_route_and_runtime_identity_fields() -> None:
    facts = _Facts(metadata={"workspace_id": "task-42"})
    outcome = _Outcome(
        result={
            "success": True,
            "status": "success",
            "exit_code": 0,
            "stdout_excerpt": "hello",
            "stderr_excerpt": "",
            "metadata": {
                "route_policy": {
                    "selected_lane": "container_scoped",
                    "selected_authority": "container_runner_transport",
                },
                "runtime_job_id": "job-1",
                "tool_command_runtime_job_id": "job-1",
                "task_runtime_job_id": "task-job-1",
                "command_id": "cmd-1",
                "runner_id": "runner-1",
            },
        },
        duration=0.25,
    )

    _append_tool_execution_record(
        facts=facts,
        outcome=outcome,
        resolved_tool_id="shell.exec",
        tool_call_id="tc-1",
        turn_sequence=4,
        workspace_id="task-42",
        artifact_refs_for_memory=(),
    )

    records = facts.metadata.get("tool_execution_records")
    assert isinstance(records, list)
    assert len(records) == 1
    record = records[0]
    assert record["tool"] == "shell.exec"
    assert record["status"] == "success"
    assert record["duration_ms"] == 250
    assert record["exit_code"] == 0
    assert record["stdout_excerpt"] == "hello"
    assert record["lane"] == "container_scoped"
    assert record["authority"] == "container_runner_transport"
    assert record["runtime_job_id"] == "job-1"
    assert record["tool_command_runtime_job_id"] == "job-1"
    assert record["task_runtime_job_id"] == "task-job-1"
    assert record["command_id"] == "cmd-1"
    assert record["runner_id"] == "runner-1"
    assert record["workspace_id"] == "task-42"


def test_append_tool_execution_record_marks_artifacts_as_runner_local_unpromoted() -> None:
    facts = _Facts(metadata={})
    outcome = _Outcome(
        result={
            "success": True,
            "status": "success",
            "exit_code": 0,
            "artifacts": ["/workspace/artifacts/scan.xml"],
            "metadata": {},
        },
        duration=0.1,
    )

    _append_tool_execution_record(
        facts=facts,
        outcome=outcome,
        resolved_tool_id="information_gathering.network_discovery.nmap",
        tool_call_id="tc-2",
        turn_sequence=5,
        workspace_id="task-5",
        artifact_refs_for_memory=[{"path": "/workspace/artifacts/scan.xml", "count": 1}],
    )

    record = facts.metadata["tool_execution_records"][0]
    assert record["artifact_scope"] == "runner_local"
    assert record["artifact_promotion_status"] == "unpromoted"
    assert record["artifact_visibility"] == "runner_workspace_only"
    assert record["artifact_refs"] == [{"path": "/workspace/artifacts/scan.xml", "count": 1}]


def test_append_tool_execution_record_marks_promoted_artifacts_as_cloud_data_plane() -> None:
    facts = _Facts(metadata={})
    outcome = _Outcome(
        result={
            "success": True,
            "status": "success",
            "exit_code": 0,
            "metadata": {},
        },
        duration=0.1,
    )

    _append_tool_execution_record(
        facts=facts,
        outcome=outcome,
        resolved_tool_id="information_gathering.network_discovery.nmap",
        tool_call_id="tc-3",
        turn_sequence=6,
        workspace_id="task-6",
        artifact_refs_for_memory=[
            {
                "path": "artifacts/scan.xml",
                "relative_path": "artifacts/scan.xml",
                "artifact_id": "artifact-1",
                "artifact_promotion_status": "upload_pending",
                "count": 1,
            }
        ],
        artifact_projection_metadata={
            "artifact_scope": "cloud_data_plane",
            "artifact_promotion_status": "upload_pending",
            "artifact_visibility": "artifact_catalog",
        },
    )

    record = facts.metadata["tool_execution_records"][0]
    assert record["artifact_scope"] == "cloud_data_plane"
    assert record["artifact_promotion_status"] == "upload_pending"
    assert record["artifact_visibility"] == "artifact_catalog"
    assert record["artifact_refs"][0]["artifact_id"] == "artifact-1"


def test_append_tool_execution_record_masks_durable_stdout_stderr_excerpts() -> None:
    sentinel = "PocSecret-DurableMasking-Sentinel-9f4c2a"
    facts = _Facts(metadata={})
    outcome = _Outcome(
        result={
            "success": True,
            "status": "success",
            "exit_code": 0,
            "stdout_excerpt": f"password={sentinel}",
            "stderr_excerpt": f"Authorization: Bearer {sentinel}",
            "metadata": {},
        },
        duration=0.1,
    )

    _append_tool_execution_record(
        facts=facts,
        outcome=outcome,
        resolved_tool_id="shell.exec",
        tool_call_id="tc-secret",
        turn_sequence=7,
        workspace_id="task-secret",
        artifact_refs_for_memory=(),
    )

    record = facts.metadata["tool_execution_records"][0]
    serialized = str(record)
    assert sentinel not in serialized
    assert "<DURABLE_SECRET_MASK:" in serialized
    assert record["stdout_excerpt"].startswith("password=<DURABLE_SECRET_MASK:")
    assert record["stderr_excerpt"].startswith("Authorization: Bearer <DURABLE_SECRET_MASK:")


def test_apply_result_state_projection_masks_tool_history_without_mutating_runtime_result() -> None:
    sentinel = "PocSecret-DurableMasking-Sentinel-9f4c2a"
    facts = _Facts(metadata={"workspace_id": "task-secret"}, iterations=3)
    projection = {
        "resolved_tool_id": "shell.exec",
        "compact_result_dict": {
            "summary": f"Authorization: Bearer {sentinel}",
        },
        "result_for_metadata": {},
        "graph_metadata": {},
        "action_record": {},
        "artifact_refs_for_memory": [
            {
                "path": "artifacts/secret-proof.txt",
                "artifact_id": "artifact-secret",
                "description": f"Authorization: Bearer {sentinel}",
            }
        ],
        "compression_usage_record": None,
    }
    captured_memory: dict[str, Any] = {}

    def _memory_reduce(**kwargs: Any) -> dict[str, Any]:
        captured_memory.update(kwargs)
        return {"recorded": kwargs}

    apply_result_state_projection(
        interactive=SimpleNamespace(trace=SimpleNamespace(usage_records=[])),
        facts=facts,
        outcome=SimpleNamespace(
            tool_id="shell.exec",
            parameters={"password": sentinel},
            result={"success": True, "metadata": {}},
            summary=f"Authorization: Bearer {sentinel}",
            duration=0.1,
        ),
        projection=projection,
        execution_id="exec-secret",
        tool_call_id="tc-secret",
        turn_sequence=8,
        compact_observation_text_fn=lambda compact, fallback=None: str(
            compact.get("summary") or fallback or ""
        ),
        refresh_trace_scratchpad_fn=lambda _interactive: None,
        memory_reduce_tool_result_fn=_memory_reduce,
        logger=SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            debug=lambda *_args, **_kwargs: None,
        ),
        safe_inc_fn=lambda _name: None,
    )

    assert sentinel in projection["compact_result_dict"]["summary"]
    history = facts.metadata["tool_execution_history"]
    serialized_history = str(history)
    assert sentinel not in serialized_history
    assert "<DURABLE_SECRET_MASK:" in serialized_history
    serialized_memory = str(captured_memory)
    assert sentinel not in serialized_memory
    assert "<DURABLE_SECRET_MASK:" in serialized_memory
    serialized_execution_records = str(facts.metadata["tool_execution_records"])
    assert sentinel not in serialized_execution_records
    assert "<DURABLE_SECRET_MASK:" in serialized_execution_records


def test_utility_projection_retains_only_operational_execution_record() -> None:
    sentinel = "TRANSIENT_UTILITY_OUTPUT_SENTINEL"
    working_memory = {"objective": {"text": "keep existing memory"}}
    facts = _Facts(
        metadata={
            "workspace_id": "task-utility",
            "working_memory": working_memory,
            "last_tool_result": {"stdout": "stale"},
            "last_tool_result_compact": {"summary": "stale"},
        },
        iterations=1,
    )
    memory_calls: list[dict[str, Any]] = []

    apply_result_state_projection(
        interactive=SimpleNamespace(trace=SimpleNamespace(usage_records=[])),
        facts=facts,
        outcome=SimpleNamespace(
            tool_id="shell.utility",
            parameters={"command": "printf transient"},
            result={
                "success": True,
                "status": "success",
                "stdout": sentinel,
                "stdout_excerpt": sentinel,
                "stderr": "",
                "exit_code": 0,
                "truncated": False,
                "metadata": {
                    "runtime_session": {"originating_capability": "utility"},
                    "semantic_observations": [{"value": sentinel}],
                },
            },
            summary=sentinel,
            duration=0.1,
        ),
        projection={
            "resolved_tool_id": "shell.utility",
            "compact_result_dict": {"summary": sentinel, "key_findings": [sentinel]},
            "result_for_metadata": {"stdout": sentinel, "summary": sentinel},
            "graph_metadata": {"result": {"stdout": sentinel}},
            "action_record": {"params": {"command": "printf transient"}},
            "artifact_refs_for_memory": [{"path": "artifacts/transient.txt"}],
            "compression_usage_record": None,
            "persistence_decision": resolve_output_persistence("shell.utility"),
        },
        execution_id="exec-utility",
        tool_call_id="tc-utility",
        turn_sequence=12,
        compact_observation_text_fn=lambda compact, fallback=None: str(
            compact.get("summary") or fallback or ""
        ),
        refresh_trace_scratchpad_fn=lambda _interactive: None,
        memory_reduce_tool_result_fn=lambda **kwargs: memory_calls.append(kwargs),
        logger=SimpleNamespace(
            warning=lambda *_args, **_kwargs: None,
            debug=lambda *_args, **_kwargs: None,
        ),
        safe_inc_fn=lambda _name: None,
    )

    assert facts.metadata["working_memory"] is working_memory
    assert memory_calls == []
    assert set(facts.metadata).isdisjoint(
        {"last_tool_result", "last_tool_result_compact", "tool_history", "action_history"}
    )
    [record] = facts.metadata["tool_execution_records"]
    assert record["tool"] == "shell.utility"
    assert record["capability"] == "utility"
    assert record["status"] == "success"
    assert record["exit_code"] == 0
    assert record["stdout_excerpt"] == ""
    assert record["stderr_excerpt"] == ""
    assert record["artifact_refs"] == []
    assert sentinel not in str(facts.metadata)


def test_apply_result_state_projection_sets_clears_and_counts_validation_errors() -> None:
    sentinel = "PocSecret-DurableMasking-Sentinel-validation-1"
    facts = _Facts(metadata={"workspace_id": "task-validation"}, iterations=2)
    increments: list[str] = []

    def _memory_reduce(**kwargs: Any) -> dict[str, Any]:
        return {"recorded": kwargs}

    def _apply(result: Mapping[str, Any]) -> None:
        apply_result_state_projection(
            interactive=SimpleNamespace(trace=SimpleNamespace(usage_records=[])),
            facts=facts,
            outcome=SimpleNamespace(
                tool_id="shell.exec",
                parameters={},
                result=result,
                summary="validation projection",
                duration=0.1,
            ),
            projection={
                "resolved_tool_id": "shell.exec",
                "compact_result_dict": {"summary": "validation projection"},
                "result_for_metadata": {},
                "graph_metadata": {},
                "action_record": {},
                "artifact_refs_for_memory": [],
                "compression_usage_record": None,
            },
            execution_id="exec-validation",
            tool_call_id="tc-validation",
            turn_sequence=10,
            compact_observation_text_fn=lambda compact, fallback=None: str(
                compact.get("summary") or fallback or ""
            ),
            refresh_trace_scratchpad_fn=lambda _interactive: None,
            memory_reduce_tool_result_fn=_memory_reduce,
            logger=SimpleNamespace(
                warning=lambda *_args, **_kwargs: None,
                debug=lambda *_args, **_kwargs: None,
            ),
            safe_inc_fn=increments.append,
        )

    _apply(
        {
            "success": False,
            "metadata": {},
            "validation_errors": [
                {"field": "password", "message": f"invalid password={sentinel}"}
            ],
        }
    )

    serialized_errors = str(facts.metadata["validation_errors"])
    assert sentinel not in serialized_errors
    assert "<DURABLE_SECRET_MASK:" in serialized_errors
    assert increments == ["langgraph_tool_validation_errors"]

    _apply({"success": True, "metadata": {}})

    assert "validation_errors" not in facts.metadata
    assert increments == ["langgraph_tool_validation_errors"]


def test_project_trace_history_masks_checkpoint_and_cache_without_masking_runtime_event() -> None:
    sentinel = "PocSecret-DurableMasking-Sentinel-cache-1"
    public_session_id = "shs_trace_secret_123"
    facts = _Facts(metadata={})
    interactive = SimpleNamespace(
        trace=SimpleNamespace(reasoning=[], observations=[], executed_tools=[]),
    )
    emitted_events: list[Mapping[str, Any]] = []
    compact_result = {
        "schema_version": "2.0",
        "tool": "shell.write_stdin",
        "status": "success",
        "success": True,
        "process_status": "running",
        "summary": f"captured password={sentinel}",
        "key_findings": [f"Authorization: Bearer {sentinel}"],
    }
    outcome = SimpleNamespace(
        tool_id="shell.write_stdin",
        parameters={"session_id": public_session_id, "chars": sentinel},
        result={"success": True, "exit_code": None, "process_status": "running"},
        summary=f"captured password={sentinel}",
        reasoning=[f"reasoned over {sentinel}"],
    )

    observation_text = project_trace_history_and_outbound_events(
        interactive=interactive,
        facts=facts,
        outcome=outcome,
        compact_result_dict=compact_result,
        result_for_metadata={"stdout": f"password={sentinel}"},
        graph_metadata={"summary": f"Authorization: Bearer {sentinel}"},
        action_record={"parameters": {"password": sentinel}},
        approval_response=None,
        tool_name="shell.exec",
        tool_call_id="tc-cache-secret",
        tool_batch_id=None,
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=9,
        sub_turn_index=None,
        interrupt_id=None,
        has_writer=True,
        writer=emitted_events.append,
        compact_observation_text_fn=lambda compact, fallback=None: str(
            compact.get("summary") or fallback or ""
        ),
        tool_execution_record_cls=SimpleNamespace,
        store_dispatch_cache_result_fn=store_dispatch_cache_result,
        tool_dispatch_cache_key="tool_dispatch_cache",
        diag_info_fn=lambda *_args, **_kwargs: None,
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
        persistence_decision=resolve_output_persistence(
            "shell.write_stdin",
            {
                "metadata": {
                    "runtime_session": {
                        "originating_capability": "assessment",
                    }
                }
            },
        ),
    )

    assert sentinel in observation_text
    assert sentinel in interactive.trace.observations[0]
    assert sentinel in str(emitted_events[0]["compact_tool_result"])
    assert emitted_events[0]["status"] == "running"
    assert emitted_events[0]["process_status"] == "running"
    serialized_trace_args = str(interactive.trace.executed_tools[0].args)
    assert sentinel not in serialized_trace_args
    assert "<DURABLE_SECRET_MASK:" in serialized_trace_args

    cache_entry = facts.metadata["tool_dispatch_cache"]["tc-cache-secret"]
    serialized_cache = str(cache_entry)
    assert sentinel not in serialized_cache
    assert "<DURABLE_SECRET_MASK:" in serialized_cache


def test_utility_dispatch_cache_excludes_transient_output_but_event_keeps_it() -> None:
    sentinel = "TRANSIENT_UTILITY_EVENT_SENTINEL"
    facts = _Facts(metadata={})
    interactive = SimpleNamespace(
        trace=SimpleNamespace(reasoning=[], observations=[], executed_tools=[]),
    )
    emitted_events: list[Mapping[str, Any]] = []
    compact_result = {
        "tool": "shell.utility",
        "status": "success",
        "success": True,
        "summary": sentinel,
        "key_findings": [sentinel],
    }
    outcome = SimpleNamespace(
        tool_id="shell.utility",
        parameters={"command": "printf transient"},
        result={"success": True, "exit_code": 0},
        summary=sentinel,
        reasoning=[sentinel],
    )

    project_trace_history_and_outbound_events(
        interactive=interactive,
        facts=facts,
        outcome=outcome,
        compact_result_dict=compact_result,
        result_for_metadata={"stdout": sentinel, "summary": sentinel},
        graph_metadata={"summary": sentinel},
        action_record={"params": {"command": "printf transient"}},
        approval_response=None,
        tool_name="shell.utility",
        tool_call_id="tc-utility-cache",
        tool_batch_id="tb-utility-cache",
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=9,
        sub_turn_index=None,
        interrupt_id=None,
        has_writer=True,
        writer=emitted_events.append,
        compact_observation_text_fn=lambda compact, fallback=None: str(
            compact.get("summary") or fallback or ""
        ),
        tool_execution_record_cls=SimpleNamespace,
        store_dispatch_cache_result_fn=store_dispatch_cache_result,
        tool_dispatch_cache_key="tool_dispatch_cache",
        diag_info_fn=lambda *_args, **_kwargs: None,
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
        persistence_decision=resolve_output_persistence("shell.utility"),
    )

    assert sentinel in str(emitted_events[0])
    assert emitted_events[0]["output_persistence"] == "transient"
    assert sentinel not in str(facts.metadata["tool_dispatch_cache"])
    assert interactive.trace.reasoning == []
    assert interactive.trace.observations == []
    assert interactive.trace.executed_tools == []


def test_shell_session_projection_preserves_continuation_fields_and_nullable_exit_code() -> None:
    public_session_id = "shs_projection_123"
    raw_result = {
        "tool": "shell.exec",
        "status": "success",
        "success": True,
        "process_status": "running",
        "session_id": public_session_id,
        "stdout": "first\n[... shell output truncated ...]\nlast",
        "stderr": "",
        "exit_code": None,
        "stdin_available": True,
        "truncated": True,
        "summary": f"Command is still running; poll session {public_session_id}.",
        "error_code": None,
    }
    compact_result = preserve_shell_session_result_fields(
        {
            "tool": "shell.exec",
            "status": "success",
            "success": True,
            "exit_code": 0,
            "summary": "llm summary",
        },
        raw_result=raw_result,
        tool_name="shell.exec",
    )

    assert compact_result["process_status"] == "running"
    assert compact_result["session_id"] == public_session_id
    assert compact_result["exit_code"] is None
    assert compact_result["stdin_available"] is True
    assert compact_result["stdout"] == raw_result["stdout"]
    assert compact_result["stderr"] == ""
    assert compact_result["truncated"] is True
    assert "omitted middle content" in compact_result["summary"]


def test_shell_session_result_sanitizer_keeps_continuation_fields() -> None:
    public_session_id = "shs_sanitized_123"
    sanitized = sanitize_tool_result_for_metadata(
        {
            "tool": "shell.write_stdin",
            "status": "success",
            "success": True,
            "process_status": "running",
            "session_id": public_session_id,
            "stdout": "delta",
            "stderr": "",
            "exit_code": None,
            "stdin_available": True,
            "truncated": False,
            "summary": "still running",
            "error_code": None,
            "metadata": {"provider_session_id": "terminal-private-123"},
        },
        compact_sanitized_result_keys=(
            "tool",
            "status",
            "success",
            "process_status",
            "session_id",
            "stdout",
            "stderr",
            "exit_code",
            "stdin_available",
            "truncated",
            "summary",
            "error_code",
        ),
        tool_name="shell.write_stdin",
    )

    assert sanitized == {
        "tool": "shell.write_stdin",
        "status": "success",
        "success": True,
        "process_status": "running",
        "session_id": public_session_id,
        "stdout": "delta",
        "stderr": "",
        "exit_code": None,
        "stdin_available": True,
        "truncated": False,
        "summary": "still running",
        "error_code": None,
    }


def test_compact_batch_metadata_preserves_shell_session_id_and_masks_arguments() -> None:
    public_session_id = "shs_compact_batch_123"
    private_session_id = "terminal-private-batch-123"
    env_value = "compact-env-value-123"
    chars_value = "yes\n"
    facts = _Facts(metadata={})
    batch = ToolBatch(
        tool_batch_id="tb-shell-compact",
        tool_calls=(
            ToolCall(
                tool_call_id="tc-shell-compact",
                tool_id="shell.write_stdin",
                parameters={
                    "session_id": public_session_id,
                    "chars": chars_value,
                    "env": {"VISIBLE": env_value},
                },
            ),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    result = BatchResult(
        tool_batch_id=batch.tool_batch_id,
        status=BatchStatus.COMPLETED,
        call_results=(
            ToolCallResult(
                tool_call_id="tc-shell-compact",
                tool_id="shell.write_stdin",
                status=ToolCallStatus.SUCCESS,
            ),
        ),
        effective_execution_strategy=ExecutionStrategy.SEQUENTIAL,
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    compact = {
        "tool": "shell.write_stdin",
        "process_status": "running",
        "session_id": public_session_id,
        "provider_session_id": private_session_id,
        "stdout": "delta",
        "stderr": "",
        "exit_code": None,
        "stdin_available": True,
        "truncated": False,
        "summary": "still running",
    }

    write_compact_batch_metadata(
        facts,
        batch=batch,
        result=result,
        compact_by_call_id={"tc-shell-compact": compact},
    )

    assert facts.metadata["last_tool_result_compact"]["session_id"] == public_session_id
    batch_compact = facts.metadata["last_tool_result_compact_batch"]["results"][0][
        "compact_tool_result"
    ]
    assert batch_compact["session_id"] == public_session_id
    serialized = str(facts.metadata)
    assert private_session_id not in serialized
    assert env_value not in serialized
    assert chars_value not in serialized


def test_dispatch_cache_preserves_public_shell_session_result_ids_only() -> None:
    public_session_id = "shs_public_replay_123"
    private_session_id = "terminal-private-123"
    env_value = "cache-env-value-123"
    facts = _Facts(metadata={})

    store_dispatch_cache_result(
        facts=facts,
        tool_dispatch_cache_key="tool_dispatch_cache",
        tool_call_id="tc-shell-session",
        compact_result_dict={
            "tool": "shell.exec",
            "status": "success",
            "success": True,
            "process_status": "running",
            "session_id": public_session_id,
            "summary": f"poll {public_session_id}",
        },
        result_for_metadata={
            "tool": "shell.exec",
            "success": True,
            "status": "success",
            "process_status": "running",
            "session_id": public_session_id,
            "stdout": f"session {public_session_id} started",
            "stderr": "",
            "exit_code": None,
            "stdin_available": True,
            "metadata": {
                "runtime_session": {
                    "session_id": public_session_id,
                    "provider_session_id": private_session_id,
                }
            },
        },
        graph_metadata={
            "tool": "shell.exec",
            "summary": f"session {public_session_id}",
            "result": {
                "tool": "shell.exec",
                "process_status": "running",
                "session_id": public_session_id,
            },
        },
        action_record={
            "tool_id": "shell.exec",
            "params": {
                "command": "sleep 10",
                "env": {"VISIBLE": env_value},
                "session_id": public_session_id,
            },
        },
        observation_text=f"session {public_session_id} provider {private_session_id}",
        reasoning_additions=[],
        outcome_parameters={
            "command": "sleep 10",
            "env": {"VISIBLE": env_value},
            "session_id": public_session_id,
        },
        outcome_success=True,
        outcome_summary=f"session {public_session_id}",
        approval_granted=True,
        approval_reason="approve",
        approval_metadata={},
    )

    cache_entry = facts.metadata["tool_dispatch_cache"]["tc-shell-session"]
    assert cache_entry["last_tool_result_compact"]["session_id"] == public_session_id
    assert cache_entry["last_tool_result"]["session_id"] == public_session_id
    assert (
        cache_entry["last_tool_result"]["metadata"]["runtime_session"]["session_id"]
        == public_session_id
    )
    assert cache_entry["tool_history_entry"]["result"]["session_id"] == public_session_id
    assert cache_entry["action_record"]["params"]["session_id"] != public_session_id
    assert cache_entry["exec_record"]["args"]["session_id"] != public_session_id
    assert public_session_id in cache_entry["last_tool_result_compact"]["summary"]

    serialized_cache = str(cache_entry)
    assert private_session_id not in serialized_cache
    assert env_value not in serialized_cache
    assert "<DURABLE_SECRET_MASK:" in serialized_cache

    replay_facts = _Facts(metadata={})
    replay_facts.metadata_copy = lambda: dict(replay_facts.metadata)  # type: ignore[attr-defined]
    replay_interactive = SimpleNamespace(
        facts=replay_facts,
        trace=SimpleNamespace(reasoning=[], observations=[], executed_tools=[]),
    )

    apply_cached_dispatch_result(replay_interactive, cache_entry, "shell.exec")

    replay_metadata = replay_interactive.facts.metadata
    assert replay_metadata["last_tool_result_compact"]["session_id"] == public_session_id
    assert replay_metadata["last_tool_result"]["session_id"] == public_session_id
    assert (
        replay_metadata["last_tool_result"]["metadata"]["runtime_session"]["session_id"]
        == public_session_id
    )


def test_shell_dispatch_cache_hit_returns_update_without_new_dispatch() -> None:
    public_session_id = "shs_cached_dispatch_789"
    cache_entry = {
        "last_tool_result_compact": {
            "tool": "shell.write_stdin",
            "process_status": "running",
            "session_id": public_session_id,
            "summary": "still running",
        },
        "last_tool_result": {
            "tool": "shell.write_stdin",
            "success": True,
            "status": "success",
            "process_status": "running",
            "session_id": public_session_id,
        },
        "observation_text": "still running",
        "exec_record": {
            "args": {"session_id": "<DURABLE_SECRET_MASK:secret>"},
            "status": "success",
            "observation": "still running",
            "reasoning": "still running",
            "approval_granted": True,
            "approval_reason": "approve",
            "approval_metadata": {},
        },
    }
    facts = _Facts(metadata={"tool_dispatch_cache": {"tc-cached": cache_entry}})
    facts.metadata_copy = lambda: dict(facts.metadata)  # type: ignore[attr-defined]
    interactive = SimpleNamespace(
        facts=facts,
        trace=SimpleNamespace(reasoning=[], observations=[], executed_tools=[]),
        as_graph_update=lambda: {"metadata": facts.metadata},
    )
    applied: list[str] = []
    cleared: list[str] = []

    update = maybe_return_cached_dispatch_update(
        interactive=interactive,
        metadata=facts.metadata,
        tool_call_id="tc-cached",
        tool_name="shell.write_stdin",
        tool_dispatch_cache_key="tool_dispatch_cache",
        apply_cached_dispatch_result_fn=lambda state, cached, tool_name: (
            applied.append(tool_name),
            apply_cached_dispatch_result(state, cached, tool_name),
        ),
        clear_tool_plan_prepared_flag_fn=lambda _state: cleared.append("plan"),
        clear_approval_gate_metadata_fn=lambda _state: cleared.append("approval"),
        log_info_fn=lambda *_args: None,
    )

    assert update == {"metadata": facts.metadata}
    assert applied == ["shell.write_stdin"]
    assert cleared == ["plan", "approval"]
    assert facts.metadata["last_tool_result"]["session_id"] == public_session_id
    assert len(interactive.trace.executed_tools) == 1


def test_dispatch_cache_masks_non_shell_session_ids_and_stdin_chars() -> None:
    public_session_id = "shs_public_argument_456"
    raw_chars = "yes please\n"
    facts = _Facts(metadata={})

    store_dispatch_cache_result(
        facts=facts,
        tool_dispatch_cache_key="tool_dispatch_cache",
        tool_call_id="tc-shell-stdin",
        compact_result_dict={
            "tool": "information_gathering.network_discovery.nmap",
            "status": "success",
            "success": True,
            "session_id": public_session_id,
            "summary": f"non-shell {public_session_id}",
        },
        result_for_metadata={
            "tool": "information_gathering.network_discovery.nmap",
            "success": True,
            "session_id": public_session_id,
            "stdout": "",
        },
        graph_metadata={"summary": f"non-shell {public_session_id}"},
        action_record={
            "tool_id": "shell.write_stdin",
            "params": {"session_id": public_session_id, "chars": raw_chars},
        },
        observation_text=f"sent {raw_chars} to {public_session_id}",
        reasoning_additions=[],
        outcome_parameters={"session_id": public_session_id, "chars": raw_chars},
        outcome_success=True,
        outcome_summary="stdin sent",
        approval_granted=True,
        approval_reason="approve",
        approval_metadata={},
    )

    cache_entry = facts.metadata["tool_dispatch_cache"]["tc-shell-stdin"]
    serialized_cache = str(cache_entry)
    assert public_session_id not in serialized_cache
    assert raw_chars not in serialized_cache
    assert "<DURABLE_SECRET_MASK:" in serialized_cache


def test_project_trace_history_keeps_secondary_compact_in_cache_only() -> None:
    facts = _Facts(metadata={})
    interactive = SimpleNamespace(
        trace=SimpleNamespace(reasoning=[], observations=[], executed_tools=[]),
    )
    emitted_events: list[Mapping[str, Any]] = []
    compact_result = {
        "schema_version": "2.0",
        "tool": "information_gathering.network_discovery.nmap",
        "status": "success",
        "success": True,
        "summary": "Primary compact summary.",
        "key_findings": ["primary finding"],
    }
    deterministic_compact_result = {
        "schema_version": "2.0",
        "tool": "information_gathering.network_discovery.nmap",
        "status": "success",
        "success": True,
        "summary": "Canonical deterministic secondary summary.",
        "key_findings": ["secondary finding"],
    }
    outcome = SimpleNamespace(
        tool_id="information_gathering.network_discovery.nmap",
        parameters={"target": "127.0.0.1"},
        result={"success": True, "exit_code": 0},
        summary="runtime fallback summary",
        reasoning=[],
    )

    project_trace_history_and_outbound_events(
        interactive=interactive,
        facts=facts,
        outcome=outcome,
        compact_result_dict=compact_result,
        result_for_metadata={"status": "success", "success": True},
        graph_metadata={"summary": "Primary compact summary."},
        action_record={"parameters": {"target": "127.0.0.1"}},
        approval_response=None,
        tool_name="information_gathering.network_discovery.nmap",
        tool_call_id="tc-secondary-cache",
        tool_batch_id="tb-secondary-cache",
        conversation_id="conv-1",
        turn_id="turn-1",
        turn_sequence=11,
        sub_turn_index=0,
        interrupt_id=None,
        has_writer=True,
        writer=emitted_events.append,
        compact_observation_text_fn=lambda compact, fallback=None: str(
            compact.get("summary") or fallback or ""
        ),
        tool_execution_record_cls=SimpleNamespace,
        store_dispatch_cache_result_fn=store_dispatch_cache_result,
        tool_dispatch_cache_key="tool_dispatch_cache",
        diag_info_fn=lambda *_args, **_kwargs: None,
        logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
        deterministic_compact_result_dict=deterministic_compact_result,
    )

    tool_end = emitted_events[0]
    assert tool_end["compact_tool_result"] == compact_result
    assert "deterministic_compact_tool_result" not in tool_end

    cache_entry = facts.metadata["tool_dispatch_cache"]["tc-secondary-cache"]
    assert cache_entry["last_tool_result_compact"] == compact_result
    assert (
        cache_entry["last_tool_result_deterministic_compact"]
        == deterministic_compact_result
    )

    replay_facts = _Facts(metadata={})
    replay_facts.metadata_copy = lambda: dict(replay_facts.metadata)  # type: ignore[attr-defined]
    replay_interactive = SimpleNamespace(
        facts=replay_facts,
        trace=SimpleNamespace(reasoning=[], observations=[], executed_tools=[]),
        as_graph_update=lambda: {},
    )
    apply_cached_dispatch_result(
        replay_interactive,
        cache_entry,
        "information_gathering.network_discovery.nmap",
    )

    replay_metadata = replay_interactive.facts.metadata
    assert replay_metadata["last_tool_result_compact"] == compact_result
    assert "last_tool_result_deterministic_compact" not in replay_metadata
    assert replay_interactive.trace.observations == ["Primary compact summary."]


def test_compact_batch_metadata_keeps_ptr_runtime_copy_raw_and_durable_copy_masked() -> None:
    sentinel = "PocSecret-DurableMasking-Sentinel-raw-ptr-4"
    facts = _Facts(metadata={})
    batch = ToolBatch(
        tool_batch_id="tb-runtime-raw-ptr",
        tool_calls=(
            ToolCall(
                tool_call_id="tc-runtime-raw-ptr",
                tool_id="shell.exec",
                parameters={"target": "127.0.0.1"},
            ),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    result = BatchResult(
        tool_batch_id=batch.tool_batch_id,
        status=BatchStatus.COMPLETED,
        call_results=(
            ToolCallResult(
                tool_call_id="tc-runtime-raw-ptr",
                tool_id="shell.exec",
                status=ToolCallStatus.SUCCESS,
            ),
        ),
        effective_execution_strategy=ExecutionStrategy.SEQUENTIAL,
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    compact = {
        "tool": "shell.exec",
        "tool_call_id": "tc-runtime-raw-ptr",
        "summary": f"captured password={sentinel}",
        "key_findings": [f"Authorization: Bearer {sentinel}"],
        "success": True,
    }

    write_compact_batch_metadata(
        facts,
        batch=batch,
        result=result,
        compact_by_call_id={"tc-runtime-raw-ptr": compact},
    )

    durable_serialized = str(facts.metadata)
    assert sentinel not in durable_serialized
    assert "<DURABLE_SECRET_MASK:" in durable_serialized

    durable_view = read_compact_evidence(facts.metadata)
    assert durable_view is not None
    assert sentinel not in str(durable_view.raw)

    runtime_view = read_compact_evidence(facts.metadata, prefer_runtime=True)
    assert runtime_view is not None
    assert sentinel in str(runtime_view.raw)

    durable_sections = extract_last_tool_sections(facts.metadata, facts)
    assert sentinel not in str(durable_sections)
    runtime_sections = extract_last_tool_sections(
        facts.metadata,
        facts,
        prefer_runtime_evidence=True,
    )
    assert sentinel in runtime_sections["tool_output_summary"]
    assert sentinel in runtime_sections["key_findings"]


def test_utility_compact_evidence_is_runtime_only() -> None:
    sentinel = "TRANSIENT_RUNTIME_ONLY_SENTINEL"
    facts = _Facts(metadata={})
    batch = ToolBatch(
        tool_batch_id="tb-utility-runtime-only",
        tool_calls=(
            ToolCall("tc-utility-runtime-only", "shell.utility", {"command": "printf x"}),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    result = BatchResult(
        tool_batch_id=batch.tool_batch_id,
        status=BatchStatus.COMPLETED,
        call_results=(
            ToolCallResult(
                tool_call_id="tc-utility-runtime-only",
                tool_id="shell.utility",
                status=ToolCallStatus.SUCCESS,
            ),
        ),
        effective_execution_strategy=ExecutionStrategy.SEQUENTIAL,
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )

    write_compact_batch_metadata(
        facts,
        batch=batch,
        result=result,
        compact_by_call_id={
            "tc-utility-runtime-only": {
                "tool": "shell.utility",
                "summary": sentinel,
                "success": True,
            }
        },
        persistence_decision_by_call_id={
            "tc-utility-runtime-only": resolve_output_persistence("shell.utility")
        },
    )

    assert facts.metadata == {"tool_batch_id": "tb-utility-runtime-only"}
    assert read_compact_evidence(facts.metadata) is None
    runtime_view = read_compact_evidence(facts.metadata, prefer_runtime=True)
    assert runtime_view is not None
    assert sentinel in str(runtime_view.raw)


def test_sanitize_artifact_refs_drops_signed_urls_and_object_keys() -> None:
    refs = _sanitize_artifact_refs_for_memory(
        [
            {
                "path": "https://example.s3.amazonaws.com/key?X-Amz-Signature=secret",
                "artifact_id": "artifact-1",
                "relative_path": "artifacts/scan.xml",
                "object_key": "tenant/key",
            }
        ]
    )

    assert refs == [
        {
            "path": "artifacts/scan.xml",
            "artifact_id": "artifact-1",
            "relative_path": "artifacts/scan.xml",
        }
    ]
