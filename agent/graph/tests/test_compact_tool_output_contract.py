"""
Baseline characterization tests for the compact tool-output contract.

These tests document the expected contract: tool execution produces a compact
envelope, no raw output in state, and artifact references are valid.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent.execution_strategy import ExecutionStrategy
from agent.graph.compression.schema import (
    ArtifactReference,
    CompactToolOutput,
    CompressionMetadata,
)
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState
from agent.graph.subgraphs.tool_execution_runtime.batch_runner import (
    write_compact_batch_metadata,
)
from agent.graph.tests._state_assertions import (
    REQUIRED_COMPACT_ENVELOPE_FIELDS,
    assert_compact_envelope_present,
    assert_no_raw_tool_output_in_state,
)
from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)
from tests.tool_execution_module_helper import (
    load_tool_execution_module,
    patch_tool_execution_attr,
)


class _StubCompactResult:
    def __init__(self, artifact_refs: List[Dict[str, Any]] | None = None) -> None:
        self._artifact_refs = list(artifact_refs or [])
        self.compression = type(
            "Compression",
            (),
            {"source": "deterministic", "fallback_reason": None},
        )()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "2.0",
            "tool": "shell.exec",
            "status": "success",
            "success": True,
            "exit_code": 0,
            "summary": "echo succeeded",
            "key_findings": ["hello"],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
            "artifact_refs": self._artifact_refs,
            "compression": {"source": "deterministic"},
        }


class _StubCompressionResult:
    def __init__(self, compact_output: _StubCompactResult) -> None:
        self.compact_output = compact_output
        self.usage_record = None


def _stub_coordinator_outcome() -> ToolExecutionOutcome:
    """Stub outcome with raw output (current pre-Phase-1 behavior)."""
    return ToolExecutionOutcome(
        tool_id="shell.exec",
        parameters={"command": "echo hello"},
        catalog=[ToolCatalogEntry(tool_id="shell.exec", name="shell", category="shell", description="")],
        result={
            "tool": "shell.exec",
            "success": True,
            "status": "success",
            "stdout": "hello\n",
            "stderr": "",
            "stdout_excerpt": "hello",
            "stderr_excerpt": "",
            "observation": "Command completed",
            "exit_code": 0,
        },
        summary="Command completed",
        reasoning=[],
        duration=0.1,
    )


class _StubCoordinator:
    async def run(self, request):  # noqa: ANN001
        return _stub_coordinator_outcome()


class _RaisingCoordinator:
    async def run(self, request):  # noqa: ANN001
        raise RuntimeError("executor failed with api_key=sk-secret-value-123456")


async def _run_tool_execution(state: Dict[str, Any], *, context: GraphRuntimeContext) -> Dict[str, Any]:
    """Load the tool-execution module lazily to avoid import-order test cycles."""
    module = load_tool_execution_module()
    writer = module.get_stream_writer()
    return await module.run_tool_execution(state, context=context, writer=writer)


def _patch_tool_execution_for_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch coordinator and stream writer so tests run outside LangGraph context."""
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)


def _patch_tool_execution_with_writer(
    monkeypatch: pytest.MonkeyPatch,
    writer_events: List[Dict[str, Any]],
) -> None:
    """Patch coordinator and stream writer with event capture."""
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),
    )

    def _writer(event: Dict[str, Any]) -> None:
        writer_events.append(event)

    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: _writer)


def _base_facts() -> FactsState:
    """Base facts with planner_plan to avoid invoking real planner."""
    return FactsState(
        task_id=1,
        message="Run echo",
        capability="simple_tool_execution",
        intent_hints={"targets": ["localhost"]},
        metadata={
            "api_key": "key",
            "model": "model",
            "tool_plan_prepared": True,
            "planner_plan": {
                "selected_tools": ["shell.exec"],
                "tool_parameters": {"shell.exec": {"command": "echo hello"}},
                "execution_strategy": "sequential",
                "reasoning": "",
                "expected_outcome": "",
                "tool_batch": {
                    "tool_batch_id": "batch-compact-single",
                    "requested_execution_strategy": "sequential",
                    "tool_calls": [
                        {
                            "tool_call_id": "call-compact-single",
                            "tool_id": "shell.exec",
                            "parameters": {"command": "echo hello"},
                            "intent": "run echo",
                        }
                    ],
                },
            },
            # Phase 5 cutover: the hot-path ConversationContextBundle is
            # required by the tool-execution request-context builder.
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-compact",
                turn_id="turn-compact",
                turn_sequence=0,
                messages=[],
            ),
        },
    )


def _base_context() -> GraphRuntimeContext:
    """Base runtime context for tool execution tests."""
    return GraphRuntimeContext(
        task_id=1,
        user_id=1,
        workspace_path="/workspace",
        feature_flags={},
        api_key="key",
        model="model",
    )


def _canonical_compact_output_dict() -> Dict[str, Any]:
    """Return a canonical compact envelope payload for graph-state tests."""
    return CompactToolOutput(
        tool="shell.exec",
        status="success",
        success=True,
        exit_code=0,
        summary="echo succeeded",
        key_findings=["hello"],
        errors=[],
        report_recommendations=[],
        structured_signals=[{"type": "kv_pair", "key": "line_count", "value": 1}],
        decision_evidence=["stdout contained hello"],
        lossiness_risk="low",
        artifact_refs=[
            ArtifactReference(
                path="/workspace/artifacts/tool-output.txt",
                artifact_id="artifact-1",
                execution_id="exec-1",
                tool_call_id="call-primary",
                tool_name="shell.exec",
                artifact_kind="stdout",
                label="Raw stdout",
                relative_path="artifacts/tool-output.txt",
            )
        ],
        compression=CompressionMetadata(source="deterministic"),
    ).to_dict()


def test_compact_tool_output_to_dict_matches_state_assertion_contract() -> None:
    """CompactToolOutput.to_dict() must remain accepted by state assertions."""
    compact = _canonical_compact_output_dict()

    assert set(compact) == REQUIRED_COMPACT_ENVELOPE_FIELDS
    assert_compact_envelope_present({"last_tool_result_compact": compact})


def test_write_compact_batch_metadata_keeps_batch_and_primary_field_shapes() -> None:
    """Batch metadata writer owns both compact graph-state fields."""
    primary_compact = _canonical_compact_output_dict()
    secondary_compact = CompactToolOutput(
        tool="http.request",
        status="success",
        success=True,
        exit_code=0,
        summary="HTTP 200 from target",
        key_findings=["status 200"],
        errors=[],
        report_recommendations=[],
        structured_signals=[{"type": "kv_pair", "key": "status_code", "value": 200}],
        decision_evidence=["GET / returned 200"],
        lossiness_risk="low",
        artifact_refs=[],
        compression=CompressionMetadata(source="deterministic"),
    ).to_dict()
    facts = SimpleNamespace(metadata={})
    batch = ToolBatch(
        tool_batch_id="batch-compact-contract",
        tool_calls=(
            ToolCall(
                tool_call_id="call-primary",
                tool_id="shell.exec",
                parameters={"command": "echo hello"},
                intent="run command",
            ),
            ToolCall(
                tool_call_id="call-secondary",
                tool_id="http.request",
                parameters={"url": "http://target/"},
                intent="check HTTP",
            ),
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
        deferred_followups=("inspect saved output",),
    )
    result = BatchResult(
        tool_batch_id=batch.tool_batch_id,
        status=BatchStatus.COMPLETED,
        call_results=(
            ToolCallResult(
                tool_call_id="call-primary",
                tool_id="shell.exec",
                status=ToolCallStatus.SUCCESS,
            ),
            ToolCallResult(
                tool_call_id="call-secondary",
                tool_id="http.request",
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
            "call-primary": primary_compact,
            "call-secondary": secondary_compact,
        },
    )

    metadata = facts.metadata
    compact_batch = metadata["last_tool_result_compact_batch"]
    assert set(compact_batch) == {
        "tool_batch_id",
        "execution_strategy",
        "requested_execution_strategy",
        "status",
        "success",
        "results",
        "deferred_followups",
    }
    assert compact_batch["tool_batch_id"] == "batch-compact-contract"
    assert compact_batch["execution_strategy"] == "sequential"
    assert compact_batch["requested_execution_strategy"] == "sequential"
    assert compact_batch["status"] == "completed"
    assert compact_batch["success"] is True
    assert compact_batch["deferred_followups"] == ["inspect saved output"]

    rows = compact_batch["results"]
    assert [row["tool_call_id"] for row in rows] == ["call-primary", "call-secondary"]
    assert all(
        set(row) == {
            "tool_call_id",
            "tool_id",
            "intent",
            "status",
            "success",
            "compact_tool_result",
        }
        for row in rows
    )
    assert rows[0]["compact_tool_result"] == primary_compact
    assert rows[1]["compact_tool_result"] == secondary_compact

    assert metadata["last_tool_result_compact"] == primary_compact
    assert metadata["last_tool_result_compact"] == rows[0]["compact_tool_result"]
    assert_compact_envelope_present(metadata)


@pytest.mark.asyncio
async def test_tool_execution_produces_compact_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool execution must produce last_tool_result_compact with required fields."""
    _patch_tool_execution_for_test(monkeypatch)
    state = InteractiveState(facts=_base_facts())

    result = await _run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)
    metadata = updated.facts.metadata

    assert_compact_envelope_present(metadata)


@pytest.mark.asyncio
async def test_no_raw_output_in_state_after_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """last_tool_result and tool_history must not contain stdout/stderr/stdout_excerpt/stderr_excerpt."""
    _patch_tool_execution_for_test(monkeypatch)
    state = InteractiveState(facts=_base_facts())

    result = await _run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)
    metadata = updated.facts.metadata

    assert_no_raw_tool_output_in_state(metadata)


@pytest.mark.asyncio
async def test_compact_envelope_has_all_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compact envelope must have schema_version, tool, status, success, exit_code, summary, etc."""
    _patch_tool_execution_for_test(monkeypatch)
    state = InteractiveState(facts=_base_facts())

    result = await _run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)
    compact = updated.facts.metadata.get("last_tool_result_compact")

    assert compact is not None
    assert_compact_envelope_present(updated.facts.metadata)
    assert compact.get("schema_version") == "2.0"
    assert compact.get("tool") == "shell.exec"
    assert "summary" in compact
    assert isinstance(compact.get("artifact_refs"), list)
    assert isinstance(compact.get("key_findings"), list)
    assert isinstance(compact.get("errors"), list)
    assert isinstance(compact.get("report_recommendations"), list)
    assert isinstance(compact.get("structured_signals"), list)
    assert isinstance(compact.get("decision_evidence"), list)
    assert compact.get("lossiness_risk") in {"low", "medium", "high"}


@pytest.mark.asyncio
async def test_callback_failure_produces_compact_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callback exceptions should produce compact metadata for downstream synthesis."""
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _RaisingCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    state = InteractiveState(facts=_base_facts())

    result = await _run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)
    metadata = updated.facts.metadata
    compact = metadata.get("last_tool_result_compact")

    assert isinstance(compact, dict)
    assert compact["tool"] == "shell.exec"
    assert compact["success"] is False
    assert compact["status"] == "executor_callback_error"
    assert "sk-secret" not in compact["summary"]
    assert "<redacted>" in compact["summary"]
    assert metadata.get("last_tool_result_compact_batch", {}).get("results")


@pytest.mark.asyncio
async def test_artifact_references_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When compact envelope exists, artifact_refs must follow ArtifactReference schema."""
    _patch_tool_execution_for_test(monkeypatch)
    state = InteractiveState(facts=_base_facts())

    result = await _run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)
    compact = updated.facts.metadata.get("last_tool_result_compact")

    assert compact is not None
    artifact_refs = compact.get("artifact_refs", [])
    for ref in artifact_refs:
        assert isinstance(ref, dict)
        assert "path" in ref
        assert isinstance(ref["path"], str)
        # artifact_id and execution_id are optional
        if "artifact_id" in ref:
            assert ref["artifact_id"] is None or isinstance(ref["artifact_id"], str)
        if "execution_id" in ref:
            assert ref["execution_id"] is None or isinstance(ref["execution_id"], str)


@pytest.mark.asyncio
async def test_compact_mode_suppresses_tool_delta_and_emits_compact_tool_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compact mode should not stream raw deltas and should include compact payload on tool_end."""
    writer_events: List[Dict[str, Any]] = []
    _patch_tool_execution_with_writer(monkeypatch, writer_events)
    patch_tool_execution_attr(
        monkeypatch,
        "save_tool_output_artifact",
        lambda **kwargs: "/workspace/artifacts/tool_output.txt",
    )

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        return _StubCompressionResult(
            _StubCompactResult(
                artifact_refs=[{"path": "/workspace/artifacts/tool_output.txt"}]
            )
        )

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    state = InteractiveState(facts=_base_facts())
    await _run_tool_execution(state.as_graph_state(), context=_base_context())

    tool_delta_events = [event for event in writer_events if event.get("type") == "tool_delta"]
    tool_end_events = [event for event in writer_events if event.get("type") == "tool_end"]
    assert tool_delta_events == []
    assert len(tool_end_events) == 1
    assert tool_end_events[0].get("compact_tool_result", {}).get("schema_version") == "2.0"
    assert tool_end_events[0].get("summary", {}).get("summary") == "echo succeeded"
    assert tool_end_events[0].get("sub_turn_index") == 0


@pytest.mark.asyncio
async def test_direct_executor_first_step_emits_consistent_sub_turn_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first direct-executor step should stamp sub_turn_index=0 on tool lifecycle events."""
    writer_events: List[Dict[str, Any]] = []
    _patch_tool_execution_with_writer(monkeypatch, writer_events)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        return _StubCompressionResult(_StubCompactResult())

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    state = InteractiveState(facts=_base_facts())
    await _run_tool_execution(state.as_graph_state(), context=_base_context())

    tool_start = next(event for event in writer_events if event.get("type") == "tool_start")
    tool_end = next(event for event in writer_events if event.get("type") == "tool_end")

    assert tool_start.get("sub_turn_index") == 0
    assert tool_end.get("sub_turn_index") == 0


@pytest.mark.asyncio
async def test_direct_executor_follow_up_step_advances_sub_turn_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sanctioned follow-up step should derive its identity from prior call_tool decisions."""
    writer_events: List[Dict[str, Any]] = []
    _patch_tool_execution_with_writer(monkeypatch, writer_events)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        return _StubCompressionResult(_StubCompactResult())

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    facts = _base_facts()
    facts.decision_history = ["call_tool: ping host first"]
    state = InteractiveState(facts=facts)
    await _run_tool_execution(state.as_graph_state(), context=_base_context())

    tool_start = next(event for event in writer_events if event.get("type") == "tool_start")
    tool_end = next(event for event in writer_events if event.get("type") == "tool_end")

    assert tool_start.get("sub_turn_index") == 1
    assert tool_end.get("sub_turn_index") == 1


@pytest.mark.asyncio
async def test_direct_executor_retry_follow_up_uses_step_identity_not_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry metadata should not replace direct-executor step identity on tool lifecycle events."""
    writer_events: List[Dict[str, Any]] = []
    _patch_tool_execution_with_writer(monkeypatch, writer_events)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        return _StubCompressionResult(_StubCompactResult())

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    facts = _base_facts()
    facts.decision_history = ["call_tool: retry with corrected parameters"]
    facts.metadata["retry_tracking"] = {"count": 4}
    state = InteractiveState(facts=facts)
    await _run_tool_execution(state.as_graph_state(), context=_base_context())

    tool_start = next(event for event in writer_events if event.get("type") == "tool_start")
    tool_end = next(event for event in writer_events if event.get("type") == "tool_end")

    assert tool_start.get("sub_turn_index") == 1
    assert tool_end.get("sub_turn_index") == 1


@pytest.mark.asyncio
async def test_raw_observation_not_written_to_history_or_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool raw observation must be replaced by compact summary in history contexts."""

    raw_xml_observation = (
        "<?xml version=\"1.0\"?><nmaprun><host><status state=\"up\"/></host></nmaprun>\n"
        "Traceback (most recent call last): sqlalchemy.exc.NotSupportedError"
    )

    class _RawObservationCoordinator:
        async def run(self, request):  # noqa: ANN001
            outcome = _stub_coordinator_outcome()
            outcome.result["observation"] = raw_xml_observation
            return outcome

    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _RawObservationCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        return _StubCompressionResult(_StubCompactResult())

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    state = InteractiveState(facts=_base_facts())
    result = await _run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)

    assert updated.trace.observations[-1] == "echo succeeded"
    assert "history" not in (updated.facts.metadata or {})
    compact_summary = str(
        (updated.facts.metadata or {}).get("last_tool_result_compact", {}).get("summary", "")
    ).lower()
    assert "<?xml" not in compact_summary
    assert "echo succeeded" in compact_summary
    combined_reasoning = "\n".join(updated.trace.reasoning or [])
    assert "traceback" not in combined_reasoning.lower()


@pytest.mark.asyncio
async def test_deep_reasoning_iteration_summary_uses_compact_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DR iteration tool summary should come from compact payload, not pre-compression outcome summary."""

    class _RawSummaryCoordinator:
        async def run(self, request):  # noqa: ANN001
            outcome = _stub_coordinator_outcome()
            outcome.summary = "RAW TEMPLATE TEXT SHOULD NOT BE USED"
            return outcome

    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _RawSummaryCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        return _StubCompressionResult(_StubCompactResult())

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    facts = _base_facts()
    facts.capability = "deep_reasoning"
    state = InteractiveState(facts=facts)
    result = await _run_tool_execution(state.as_graph_state(), context=_base_context())
    updated = InteractiveState.from_mapping(result)

    records = (updated.facts.metadata or {}).get("dr_iteration_records") or {}
    record = records.get("1") or {}
    tool_record = record.get("tool") or {}
    assert tool_record.get("summary") == "echo succeeded"
    assert "RAW TEMPLATE TEXT" not in str(tool_record.get("summary", ""))
