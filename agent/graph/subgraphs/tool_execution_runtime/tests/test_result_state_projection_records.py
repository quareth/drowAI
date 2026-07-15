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
from agent.graph.subgraphs.tool_execution_runtime.approval_and_idempotency import (
    store_dispatch_cache_result,
)
from agent.graph.subgraphs.tool_execution_runtime.result_state_projection import (
    _append_tool_execution_record,
    _sanitize_artifact_refs_for_memory,
    apply_result_state_projection,
    project_trace_history_and_outbound_events,
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


@dataclass
class _Facts:
    metadata: dict[str, Any]
    iterations: int = 1
    task_id: int = 1


@dataclass
class _Outcome:
    result: Mapping[str, Any]
    duration: float


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


def test_project_trace_history_masks_dispatch_cache_without_masking_runtime_event() -> None:
    sentinel = "PocSecret-DurableMasking-Sentinel-cache-1"
    facts = _Facts(metadata={})
    interactive = SimpleNamespace(
        trace=SimpleNamespace(reasoning=[], observations=[], executed_tools=[]),
    )
    emitted_events: list[Mapping[str, Any]] = []
    compact_result = {
        "schema_version": "2.0",
        "tool": "shell.exec",
        "status": "success",
        "success": True,
        "summary": f"captured password={sentinel}",
        "key_findings": [f"Authorization: Bearer {sentinel}"],
    }
    outcome = SimpleNamespace(
        tool_id="shell.exec",
        parameters={"password": sentinel},
        result={"success": True, "exit_code": 0},
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
    )

    assert sentinel in observation_text
    assert sentinel in interactive.trace.observations[0]
    assert sentinel in str(emitted_events[0]["compact_tool_result"])

    cache_entry = facts.metadata["tool_dispatch_cache"]["tc-cache-secret"]
    serialized_cache = str(cache_entry)
    assert sentinel not in serialized_cache
    assert "<DURABLE_SECRET_MASK:" in serialized_cache


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
