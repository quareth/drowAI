"""Regression tests for per-call isolation in tool-batch execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from agent.config import AgentConfig
from agent.execution_strategy import ExecutionStrategy
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState
from agent.models import ExecutionResult
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from agent.tool_runtime import ToolCatalogEntry, ToolExecutionOutcome
from agent.tool_runtime.batch.validator import BatchValidationResult
from backend.services.runtime_provider.contracts import (
    RuntimeOperationStatus,
    build_runtime_result,
)
from tests.tool_execution_module_helper import patch_tool_execution_attr


TOOL_ID = "information_gathering.network_discovery.nmap"


@dataclass
class _StubCompression:
    source: str = "deterministic"
    fallback_reason: str | None = None


class _StubCompactResult:
    def __init__(self, *, tool: str, summary: str) -> None:
        self._tool = tool
        self._summary = summary
        self.compression = _StubCompression()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "2.0",
            "tool": self._tool,
            "status": "success",
            "success": True,
            "exit_code": 0,
            "summary": self._summary,
            "key_findings": [],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
            "artifact_refs": [],
            "compression": {"source": "deterministic"},
        }


class _StubCompressionResult:
    def __init__(self, compact_output: _StubCompactResult) -> None:
        self.compact_output = compact_output
        self.usage_record = None


class _PortEchoCoordinator:
    async def run(self, request: Any) -> ToolExecutionOutcome:
        planner_plan = request.metadata["planner_plan"]
        params = dict(planner_plan["tool_batch"]["tool_calls"][0]["parameters"])
        port = str(params["ports"])
        return ToolExecutionOutcome(
            tool_id=TOOL_ID,
            parameters=params,
            catalog=[
                ToolCatalogEntry(
                    tool_id=TOOL_ID,
                    name="nmap",
                    category="network_discovery",
                    description="nmap",
                )
            ],
            result={
                "tool": TOOL_ID,
                "success": True,
                "status": "success",
                "stdout": f"PORT_{port}_OUTPUT",
                "stderr": "",
                "stdout_excerpt": f"PORT_{port}_OUTPUT",
                "stderr_excerpt": "",
                "exit_code": 0,
                "duration": 0.01,
                "metadata": {},
            },
            summary=f"port {port} scanned",
            reasoning=[],
            duration=0.01,
        )


class _CoordinatedPortEchoCoordinator(_PortEchoCoordinator):
    """Force the first manifest call to finish after the second has started."""

    def __init__(self) -> None:
        self._second_call_started = asyncio.Event()
        self.first_call_observed_overlap = True

    async def run(self, request: Any) -> ToolExecutionOutcome:
        planner_plan = request.metadata["planner_plan"]
        params = dict(planner_plan["tool_batch"]["tool_calls"][0]["parameters"])
        port = str(params["ports"])
        if port == "80":
            try:
                await asyncio.wait_for(self._second_call_started.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                self.first_call_observed_overlap = False
        elif port == "443":
            self._second_call_started.set()
            await asyncio.sleep(0)
        return await super().run(request)


def _planner_plan(strategy: ExecutionStrategy) -> Dict[str, Any]:
    strategy_text = strategy.value
    return {
        "selected_tools": [TOOL_ID, TOOL_ID],
        "tool_parameters": {TOOL_ID: {"target": "172.17.0.1", "ports": "80"}},
        "execution_strategy": strategy_text,
        "reasoning": "",
        "expected_outcome": "",
        "tool_batch": {
            "tool_batch_id": "tb_call_isolation",
            "requested_execution_strategy": strategy_text,
            "deferred_followups": [],
            "selection_rationale": "scan ports separately",
            "tool_calls": [
                {
                    "tool_call_id": "tc_port_80",
                    "tool_id": TOOL_ID,
                    "parameters": {
                        "target": "172.17.0.1",
                        "ports": "80",
                        "service_detection": True,
                    },
                    "intent": "scan 80",
                },
                {
                    "tool_call_id": "tc_port_443",
                    "tool_id": TOOL_ID,
                    "parameters": {
                        "target": "172.17.0.1",
                        "ports": "443",
                        "service_detection": True,
                    },
                    "intent": "scan 443",
                },
            ],
        },
    }


def _state(strategy: ExecutionStrategy) -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=77,
            message="scan 80 and 443 separately",
            capability="simple_tool_execution",
            metadata={
                "api_key": "key",
                "model": "model",
                "runtime_placement_mode": "local",
                "tool_plan_prepared": True,
                "planner_plan": _planner_plan(strategy),
                "last_tool_result": {"stdout": "STALE_OUTPUT_SHOULD_NOT_BE_COMPRESSED"},
                METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                    conversation_id="conv-call-isolation",
                    turn_id="turn-call-isolation",
                    turn_sequence=1,
                    messages=[],
                    current_message="scan 80 and 443 separately",
                ),
            },
        )
    )


def _mixed_lane_runner_state() -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=88,
            message="run shell and look up cve plus artifact search",
            capability="simple_tool_execution",
            metadata={
                "api_key": "key",
                "model": "model",
                "tenant_id": 3,
                "runtime_placement_mode": "runner",
                "workspace_id": "task-88",
                "actor_type": "agent",
                "actor_id": "langgraph",
                "tool_plan_prepared": True,
                "planner_plan": {
                    "selected_tools": [
                        "shell.exec",
                        "knowledge.cve_lookup",
                        "artifact.search",
                    ],
                    "tool_parameters": {
                        "shell.exec": {"command": "echo runner"},
                        "knowledge.cve_lookup": {
                            "product": "openssl",
                            "version": "1.1.1",
                        },
                        "artifact.search": {"query": "ioc"},
                    },
                    "execution_strategy": "sequential",
                    "reasoning": "",
                    "expected_outcome": "",
                    "tool_batch": {
                        "tool_batch_id": "tb_mixed_lane",
                        "requested_execution_strategy": "sequential",
                        "deferred_followups": [],
                        "selection_rationale": "mixed lane validation",
                        "tool_calls": [
                            {
                                "tool_call_id": "tc_shell",
                                "tool_id": "shell.exec",
                                "parameters": {"command": "echo runner"},
                                "intent": "runner lane",
                            },
                            {
                                "tool_call_id": "tc_backend",
                                "tool_id": "knowledge.cve_lookup",
                                "parameters": {
                                    "product": "openssl",
                                    "version": "1.1.1",
                                },
                                "intent": "backend lane",
                            },
                            {
                                "tool_call_id": "tc_artifact",
                                "tool_id": "artifact.search",
                                "parameters": {"query": "ioc"},
                                "intent": "artifact lane",
                            },
                        ],
                    },
                },
                METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                    conversation_id="conv-mixed-lane",
                    turn_id="turn-mixed-lane",
                    turn_sequence=1,
                    messages=[],
                    current_message="run mixed lane batch",
                ),
            },
        )
    )


def _runner_cancelled_state() -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=89,
            message="run runner shell command",
            capability="simple_tool_execution",
            metadata={
                "api_key": "key",
                "model": "model",
                "tenant_id": 3,
                "runtime_placement_mode": "runner",
                "workspace_id": "task-89",
                "actor_type": "agent",
                "actor_id": "langgraph",
                "tool_plan_prepared": True,
                "planner_plan": {
                    "selected_tools": ["shell.exec"],
                    "tool_parameters": {"shell.exec": {"command": "echo cancel"}},
                    "execution_strategy": "sequential",
                    "reasoning": "",
                    "expected_outcome": "",
                    "tool_batch": {
                        "tool_batch_id": "tb_runner_cancelled",
                        "requested_execution_strategy": "sequential",
                        "deferred_followups": [],
                        "selection_rationale": "runner cancellation projection",
                        "tool_calls": [
                            {
                                "tool_call_id": "tc_shell",
                                "tool_id": "shell.exec",
                                "parameters": {"command": "echo cancel"},
                                "intent": "runner lane",
                            }
                        ],
                    },
                },
                METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                    conversation_id="conv-runner-cancelled",
                    turn_id="turn-runner-cancelled",
                    turn_sequence=1,
                    messages=[],
                    current_message="run cancelled runner batch",
                ),
            },
        )
    )


def _runner_parallel_cancelled_state() -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=90,
            message="run cancelled parallel runner shell commands",
            capability="simple_tool_execution",
            metadata={
                "api_key": "key",
                "model": "model",
                "tenant_id": 3,
                "runtime_placement_mode": "runner",
                "workspace_id": "task-90",
                "actor_type": "agent",
                "actor_id": "langgraph",
                "tool_plan_prepared": True,
                "planner_plan": {
                    "selected_tools": ["shell.exec", "shell.exec"],
                    "tool_parameters": {"shell.exec": {"command": "echo cancel"}},
                    "execution_strategy": "parallel",
                    "reasoning": "",
                    "expected_outcome": "",
                    "tool_batch": {
                        "tool_batch_id": "tb_runner_parallel_cancelled",
                        "requested_execution_strategy": "parallel",
                        "deferred_followups": [],
                        "selection_rationale": "runner parallel cancellation projection",
                        "tool_calls": [
                            {
                                "tool_call_id": "tc_shell_a",
                                "tool_id": "shell.exec",
                                "parameters": {"command": "echo cancel a"},
                                "intent": "runner lane",
                            },
                            {
                                "tool_call_id": "tc_shell_b",
                                "tool_id": "shell.exec",
                                "parameters": {"command": "echo cancel b"},
                                "intent": "runner lane",
                            },
                        ],
                    },
                },
                METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                    conversation_id="conv-runner-parallel-cancelled",
                    turn_id="turn-runner-parallel-cancelled",
                    turn_sequence=1,
                    messages=[],
                    current_message="run cancelled parallel runner batch",
                ),
            },
        )
    )


def _force_parallel_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.orchestrator.validate_batch",
        lambda batch, **_kwargs: BatchValidationResult(
            admitted=True,
            batch=batch,
            requested_execution_strategy=batch.requested_execution_strategy,
            effective_execution_strategy=ExecutionStrategy.PARALLEL,
            strategy_downgraded=False,
        ),
    )


def _patch_streamless_no_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)
    patch_tool_execution_attr(
        monkeypatch,
        "record_provenance_execution_start_service",
        lambda **_kwargs: None,
    )


def _cached_success_entry() -> Dict[str, Any]:
    return {
        "last_tool_result_compact": _StubCompactResult(
            tool=TOOL_ID,
            summary="compact:CACHED_80_OUTPUT",
        ).to_dict(),
        "last_tool_result": {
            "tool": TOOL_ID,
            "status": "success",
            "success": True,
            "exit_code": 0,
            "parameters": {"target": "172.17.0.1", "ports": "80"},
        },
        "action_record": {
            "tool_id": TOOL_ID,
            "params": {"target": "172.17.0.1", "ports": "80"},
            "turn_sequence": 1,
        },
        "tool_execution_history": [],
        "tool_catalog": {"entries": [], "capability": "simple_tool_execution"},
        "observation_text": "cached 80 observation",
        "reasoning_additions": ["cached 80 reasoning"],
        "exec_record": {
            "args": {"target": "172.17.0.1", "ports": "80"},
            "status": "success",
            "observation": "cached 80 observation",
            "reasoning": "cached 80",
            "approval_granted": True,
            "approval_reason": "approve",
            "approval_metadata": {},
        },
    }


def _cached_cancelled_entry() -> Dict[str, Any]:
    return {
        "last_tool_result_compact": {
            "schema_version": "2.0",
            "tool": "shell.exec",
            "status": "cancelled",
            "success": False,
            "exit_code": 130,
            "summary": "runner cancelled",
            "key_findings": [],
            "errors": ["tool cancelled"],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
            "artifact_refs": [],
            "compression": {"source": "deterministic"},
        },
        "last_tool_result": {
            "tool": "shell.exec",
            "status": "cancelled",
            "success": False,
            "stderr": "Tool command result waiter was cancelled.",
            "exit_code": 130,
            "metadata": {"error_code": "TOOL_RESULT_CANCELLED"},
            "parameters": {"command": "echo cancel"},
        },
        "action_record": {
            "tool_id": "shell.exec",
            "params": {"command": "echo cancel"},
            "turn_sequence": 1,
        },
        "tool_execution_history": [],
        "tool_catalog": {"entries": [], "capability": "simple_tool_execution"},
        "observation_text": "runner cancelled",
        "reasoning_additions": ["runner cancelled"],
        "exec_record": {
            "args": {"command": "echo cancel"},
            "status": "cancelled",
            "observation": "runner cancelled",
            "reasoning": "runner cancelled",
            "approval_granted": True,
            "approval_reason": "approve",
            "approval_metadata": {},
        },
    }


@pytest.mark.parametrize(
    "strategy",
    [ExecutionStrategy.SEQUENTIAL, ExecutionStrategy.PARALLEL],
)
@pytest.mark.asyncio
async def test_same_tool_batch_compresses_each_call_output_independently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    strategy: ExecutionStrategy,
) -> None:
    captured_raw_results: List[Dict[str, Any]] = []

    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _PortEchoCoordinator(),
    )
    patch_tool_execution_attr(monkeypatch, "get_stream_writer", lambda: None)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        raw_result = dict(kwargs["raw_result"])
        captured_raw_results.append(raw_result)
        return _StubCompressionResult(
            _StubCompactResult(
                tool=str(kwargs["tool_name"]),
                summary=f"compact:{raw_result.get('stdout')}",
            )
        )

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        _state(strategy).as_graph_state(),
        context=GraphRuntimeContext(
            task_id=77,
            user_id=1,
            workspace_path=str(tmp_path),
            api_key="key",
            model="model",
            turn_sequence=1,
        ),
    )

    raw_by_call_id = {
        str(item["tool_call_id"]): item for item in captured_raw_results
    }
    assert raw_by_call_id["tc_port_80"]["stdout"] == "PORT_80_OUTPUT"
    assert raw_by_call_id["tc_port_80"]["parameters"]["ports"] == "80"
    assert raw_by_call_id["tc_port_443"]["stdout"] == "PORT_443_OUTPUT"
    assert raw_by_call_id["tc_port_443"]["parameters"]["ports"] == "443"
    assert all("STALE_OUTPUT" not in str(item) for item in captured_raw_results)

    updated = InteractiveState.from_mapping(result)
    batch_compact = updated.facts.metadata["last_tool_result_compact_batch"]
    rows = {row["tool_call_id"]: row for row in batch_compact["results"]}
    assert rows["tc_port_80"]["compact_tool_result"]["summary"] == "compact:PORT_80_OUTPUT"
    assert rows["tc_port_443"]["compact_tool_result"]["summary"] == "compact:PORT_443_OUTPUT"
    assert updated.facts.metadata["last_tool_result"]["parameters"]["ports"] == "80"
    assert updated.facts.metadata["last_tool_result_compact"]["summary"] == (
        "compact:PORT_80_OUTPUT"
    )
    ledger = updated.facts.metadata["working_memory"]["current_turn_phases"]
    assert len(ledger) == 1
    assert [section["heading"] for section in ledger[0]["sections"]] == [
        "Tool Executed",
        "Tool Output Summary",
        "Batch Tool Results",
        "Compression Lossiness",
        "Artifact References",
    ]
    assert "ports=80" in ledger[0]["sections"][0]["body"]
    assert "summary=compact:PORT_80_OUTPUT" in ledger[0]["sections"][2]["body"]
    assert "summary=compact:PORT_443_OUTPUT" in ledger[0]["sections"][2]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy",
    (ExecutionStrategy.SEQUENTIAL, ExecutionStrategy.PARALLEL),
)
async def test_tool_execution_propagates_tool_output_compression_refusal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    strategy: ExecutionStrategy,
) -> None:
    """The wired tool execution path must preserve compression refusals."""
    refusal = LLMRefusalError(
        "declined",
        outcome=LLMRefusalOutcome(provider="openai", model="gpt-4o-mini"),
    )
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _PortEchoCoordinator(),
    )
    _patch_streamless_no_provenance(monkeypatch)
    if strategy is ExecutionStrategy.PARALLEL:
        _force_parallel_validation(monkeypatch)

    async def _refusing_compression(**_kwargs: Any) -> Any:
        raise refusal

    patch_tool_execution_attr(
        monkeypatch,
        "compress_tool_output",
        _refusing_compression,
    )

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    with pytest.raises(LLMRefusalError) as exc_info:
        await run_tool_execution(
            _state(strategy).as_graph_state(),
            context=GraphRuntimeContext(
                task_id=77,
                user_id=1,
                workspace_path=str(tmp_path),
                api_key="key",
                model="model",
                turn_sequence=1,
            ),
        )

    assert exc_info.value is refusal


@pytest.mark.asyncio
async def test_parallel_call_helpers_do_not_observe_sibling_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed_metadata_by_port: Dict[str, str] = {}
    coordinator = _CoordinatedPortEchoCoordinator()

    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: coordinator,
    )
    _patch_streamless_no_provenance(monkeypatch)
    _force_parallel_validation(monkeypatch)

    def _save_artifact_probe(**kwargs: Any) -> None:
        outcome = kwargs["outcome"]
        facts = kwargs["facts"]
        port = str(outcome.parameters["ports"])
        observed_metadata_by_port[port] = str(
            (facts.metadata or {}).get("tool_call_id")
        )
        return None

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        raw_result = dict(kwargs["raw_result"])
        return _StubCompressionResult(
            _StubCompactResult(
                tool=str(kwargs["tool_name"]),
                summary=f"compact:{raw_result.get('stdout')}",
            )
        )

    patch_tool_execution_attr(
        monkeypatch,
        "save_execution_artifact_service",
        _save_artifact_probe,
    )
    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    await run_tool_execution(
        _state(ExecutionStrategy.PARALLEL).as_graph_state(),
        context=GraphRuntimeContext(
            task_id=77,
            user_id=1,
            workspace_path=str(tmp_path),
            api_key="key",
            model="model",
            turn_sequence=1,
        ),
    )

    assert coordinator.first_call_observed_overlap
    assert observed_metadata_by_port == {
        "80": "tc_port_80",
        "443": "tc_port_443",
    }


@pytest.mark.asyncio
async def test_parallel_cached_and_fresh_calls_apply_in_manifest_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_raw_results: List[Dict[str, Any]] = []
    observed_metadata_by_port: Dict[str, str] = {}

    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _PortEchoCoordinator(),
    )
    _patch_streamless_no_provenance(monkeypatch)
    _force_parallel_validation(monkeypatch)

    def _save_artifact_probe(**kwargs: Any) -> None:
        outcome = kwargs["outcome"]
        facts = kwargs["facts"]
        port = str(outcome.parameters["ports"])
        observed_metadata_by_port[port] = str(
            (facts.metadata or {}).get("tool_call_id")
        )
        return None

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        raw_result = dict(kwargs["raw_result"])
        captured_raw_results.append(raw_result)
        return _StubCompressionResult(
            _StubCompactResult(
                tool=str(kwargs["tool_name"]),
                summary=f"compact:{raw_result.get('stdout')}",
            )
        )

    patch_tool_execution_attr(
        monkeypatch,
        "save_execution_artifact_service",
        _save_artifact_probe,
    )
    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    state = _state(ExecutionStrategy.PARALLEL)
    state.facts.metadata["tool_dispatch_cache"] = {"tc_port_80": _cached_success_entry()}

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        state.as_graph_state(),
        context=GraphRuntimeContext(
            task_id=77,
            user_id=1,
            workspace_path=str(tmp_path),
            api_key="key",
            model="model",
            turn_sequence=1,
        ),
    )

    assert [item["tool_call_id"] for item in captured_raw_results] == ["tc_port_443"]
    assert observed_metadata_by_port == {"443": "tc_port_443"}

    updated = InteractiveState.from_mapping(result)
    batch_compact = updated.facts.metadata["last_tool_result_compact_batch"]
    rows = {row["tool_call_id"]: row for row in batch_compact["results"]}
    assert rows["tc_port_80"]["compact_tool_result"]["summary"] == (
        "compact:CACHED_80_OUTPUT"
    )
    assert rows["tc_port_443"]["compact_tool_result"]["summary"] == (
        "compact:PORT_443_OUTPUT"
    )
    assert updated.facts.metadata["last_tool_result"]["parameters"]["ports"] == "80"
    assert updated.facts.metadata["last_tool_result_compact"]["summary"] == (
        "compact:CACHED_80_OUTPUT"
    )
    ledger = updated.facts.metadata["working_memory"]["current_turn_phases"]
    assert len(ledger) == 1
    assert "summary=compact:CACHED_80_OUTPUT" in ledger[0]["sections"][2]["body"]
    assert "summary=compact:PORT_443_OUTPUT" in ledger[0]["sections"][2]["body"]
    assert set(updated.facts.metadata["tool_dispatch_cache"]) == {
        "tc_port_80",
        "tc_port_443",
    }
    assert updated.facts.tool_calls_used == 1


@pytest.mark.asyncio
async def test_missing_orchestrator_placement_fails_before_dispatch_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    callback_builds: List[str] = []

    def _missing_runtime_request_context(*, interactive: Any, context: Any, metadata: Dict[str, Any]):
        request = SimpleNamespace(
            metadata=metadata,
            workspace_path=str(tmp_path),
            task_id=77,
        )
        coordinator_config = AgentConfig(
            task_id="77",
            workspace_path=str(tmp_path),
            model_name="model",
        )
        coordinator_config.runtime_placement_mode = None
        runtime_context = SimpleNamespace(runtime_placement_mode=None, turn_sequence=1)
        return request, coordinator_config, runtime_context, str(tmp_path)

    def _build_callback_must_not_run(**_kwargs: Any):
        callback_builds.append("callback")
        raise AssertionError("missing placement must fail before dispatch callback setup")

    patch_tool_execution_attr(
        monkeypatch,
        "build_request_and_coordinator_config",
        _missing_runtime_request_context,
    )
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.orchestrator.build_run_one_call_callback",
        _build_callback_must_not_run,
    )

    state = _state(ExecutionStrategy.SEQUENTIAL)
    state.facts.metadata.pop("runtime_placement_mode", None)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(state.as_graph_state(), context=None)

    assert callback_builds == []
    updated = InteractiveState.from_mapping(result)
    compact_batch = updated.facts.metadata["last_tool_result_compact_batch"]
    assert compact_batch["status"] == "failed"
    assert compact_batch["success"] is False
    assert [
        row["failure_category"] for row in compact_batch["results"]
    ] == ["missing_runtime_placement", "missing_runtime_placement"]
    assert (
        updated.facts.metadata["last_tool_result_compact"]["status"]
        == "missing_runtime_placement"
    )


@pytest.mark.asyncio
async def test_mixed_lane_batch_uses_batch_executor_and_per_call_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    call_order: List[tuple[str, str]] = []
    runner_requests: List[Any] = []

    class _Provider:
        async def send_tool_command(self, request: Any) -> Any:
            runner_requests.append(request)
            call_order.append(("runner", str(request.payload.get("tool"))))
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "delegate_result": {
                        "success": True,
                        "stdout": "runner-ok",
                        "stderr": "",
                        "exit_code": 0,
                        "status": "success",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode: Any) -> _Provider:
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    class _LocalExecutor:
        config = AgentConfig(task_id="88", workspace_path=str(tmp_path))

        def _tool_to_shell_command(self, tool_id: str, parameters: Dict[str, Any]) -> List[str]:
            return ["sh", "-lc", str(parameters.get("command") or "true")]

        async def _maybe_request_approval(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        async def _execute_single_tool(
            self,
            tool_id: str,
            _parameters: Dict[str, Any],
            **_kwargs: Any,
        ) -> ExecutionResult:
            call_order.append(("local", tool_id))
            return ExecutionResult(success=True, stdout=f"{tool_id}-ok", stderr="", exit_code=0)

    local_executor = _LocalExecutor()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.GraphToolExecutor._get_executor",
        lambda self, workspace_path, task_id, model, runtime_placement_mode="local": local_executor,
    )
    _patch_streamless_no_provenance(monkeypatch)
    patch_tool_execution_attr(monkeypatch, "save_execution_artifact_service", lambda **_kwargs: None)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        raw_result = dict(kwargs["raw_result"])
        status = str(raw_result.get("status") or "unknown")
        return _StubCompressionResult(
            _StubCompactResult(
                tool=str(kwargs["tool_name"]),
                summary=f"compact:{status}",
            )
        )

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        _mixed_lane_runner_state().as_graph_state(),
        context=GraphRuntimeContext(
            task_id=88,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-88",
            actor_type="agent",
            actor_id="langgraph",
            workspace_path=str(tmp_path),
            model="model",
        ),
    )

    assert len(runner_requests) == 1
    assert runner_requests[0].payload["tool"] == "shell.exec"
    assert call_order == [
        ("runner", "shell.exec"),
        ("local", "knowledge.cve_lookup"),
        ("local", "artifact.search"),
    ]

    updated = InteractiveState.from_mapping(result)
    call_ids = [
        row["tool_call_id"]
        for row in updated.facts.metadata["last_tool_result_compact_batch"]["results"]
    ]
    assert call_ids == ["tc_shell", "tc_backend", "tc_artifact"]


@pytest.mark.asyncio
async def test_runner_cancelled_delegate_result_projects_cancelled_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Provider:
        async def send_tool_command(self, request: Any) -> Any:
            return build_runtime_result(
                request,
                accepted=False,
                provider="fake",
                status=RuntimeOperationStatus.FAILED,
                error_code="TOOL_RESULT_CANCELLED",
                error_message="Tool command result waiter was cancelled.",
                metadata={
                    "delegate_result": {
                        "success": False,
                        "stdout": "",
                        "stderr": "Tool command result waiter was cancelled.",
                        "exit_code": 130,
                        "status": "cancelled",
                        "error_code": "TOOL_RESULT_CANCELLED",
                        "error_message": "Tool command result waiter was cancelled.",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode: Any) -> _Provider:
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    _patch_streamless_no_provenance(monkeypatch)
    patch_tool_execution_attr(monkeypatch, "save_execution_artifact_service", lambda **_kwargs: None)

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        raw_result = dict(kwargs["raw_result"])
        status = str(raw_result.get("status") or "unknown")
        return _StubCompressionResult(
            _StubCompactResult(
                tool=str(kwargs["tool_name"]),
                summary=f"compact:{status}",
            )
        )

    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        _runner_cancelled_state().as_graph_state(),
        context=GraphRuntimeContext(
            task_id=89,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-89",
            actor_type="agent",
            actor_id="langgraph",
            workspace_path=str(tmp_path),
            model="model",
        ),
    )

    updated = InteractiveState.from_mapping(result)
    rows = {
        row["tool_call_id"]: row
        for row in updated.facts.metadata["last_tool_result_compact_batch"]["results"]
    }
    assert rows["tc_shell"]["status"] == "cancelled"
    assert rows["tc_shell"]["failure_category"] == "cancelled"


@pytest.mark.asyncio
async def test_cached_cancelled_dispatch_projects_cancelled_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_calls: List[Any] = []

    class _Provider:
        async def send_tool_command(self, request: Any) -> Any:
            provider_calls.append(request)
            raise AssertionError("cached dispatch should skip provider call")

    class _Registry:
        def get_provider(self, *, runtime_placement_mode: Any) -> _Provider:
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    _patch_streamless_no_provenance(monkeypatch)

    state = _runner_cancelled_state()
    state.facts.metadata["tool_dispatch_cache"] = {"tc_shell": _cached_cancelled_entry()}

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        state.as_graph_state(),
        context=GraphRuntimeContext(
            task_id=89,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-89",
            actor_type="agent",
            actor_id="langgraph",
            workspace_path=str(tmp_path),
            model="model",
        ),
    )

    assert provider_calls == []
    updated = InteractiveState.from_mapping(result)
    rows = {
        row["tool_call_id"]: row
        for row in updated.facts.metadata["last_tool_result_compact_batch"]["results"]
    }
    assert rows["tc_shell"]["status"] == "cancelled"
    assert rows["tc_shell"]["failure_category"] == "cancelled"


@pytest.mark.asyncio
async def test_approval_rejection_skips_runner_dispatch_for_all_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_calls: List[Any] = []
    local_calls: List[str] = []

    class _Provider:
        async def send_tool_command(self, request: Any) -> Any:
            provider_calls.append(request)
            raise AssertionError("approval rejection must not dispatch runner tool.command")

    class _Registry:
        def get_provider(self, *, runtime_placement_mode: Any) -> _Provider:
            assert runtime_placement_mode.value == "runner"
            return _Provider()

        class _LocalExecutor:
            config = AgentConfig(task_id="88", workspace_path=str(tmp_path))

            def _tool_to_shell_command(self, tool_id: str, parameters: Dict[str, Any]) -> List[str]:
                return ["sh", "-lc", str(parameters.get("command") or "true")]

            async def _maybe_request_approval(self, *_args: Any, **_kwargs: Any) -> bool:
                return True

        async def _execute_single_tool(
            self,
            tool_id: str,
            _parameters: Dict[str, Any],
            **_kwargs: Any,
        ) -> ExecutionResult:
            local_calls.append(tool_id)
            raise AssertionError("approval rejection must skip local call execution")

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.GraphToolExecutor._get_executor",
        lambda self, workspace_path, task_id, model, runtime_placement_mode="local": _LocalExecutor(),
    )
    patch_tool_execution_attr(monkeypatch, "should_require_approval", lambda _metadata: True)
    patch_tool_execution_attr(
        monkeypatch,
        "request_tool_approval",
        lambda **_kwargs: {"action": "skip"},
    )
    _patch_streamless_no_provenance(monkeypatch)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        _mixed_lane_runner_state().as_graph_state(),
        context=GraphRuntimeContext(
            task_id=88,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-88",
            actor_type="agent",
            actor_id="langgraph",
            workspace_path=str(tmp_path),
            model="model",
        ),
    )

    assert provider_calls == []
    assert local_calls == []
    updated = InteractiveState.from_mapping(result)
    rows = {
        row["tool_call_id"]: row
        for row in updated.facts.metadata["last_tool_result_compact_batch"]["results"]
    }
    assert rows["tc_shell"]["status"] == "denied"
    assert rows["tc_backend"]["status"] == "denied"
    assert rows["tc_artifact"]["status"] == "denied"


@pytest.mark.asyncio
async def test_resume_uses_approved_subset_without_reprompt_or_extra_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_calls: List[Any] = []
    local_calls: List[str] = []

    class _Provider:
        async def send_tool_command(self, request: Any) -> Any:
            provider_calls.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "delegate_result": {
                        "success": True,
                        "stdout": "runner-ok",
                        "stderr": "",
                        "exit_code": 0,
                        "status": "success",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode: Any) -> _Provider:
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    class _LocalExecutor:
        config = AgentConfig(task_id="88", workspace_path=str(tmp_path))

        def _tool_to_shell_command(self, tool_id: str, parameters: Dict[str, Any]) -> List[str]:
            return ["sh", "-lc", str(parameters.get("command") or "true")]

        async def _maybe_request_approval(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        async def _execute_single_tool(
            self,
            tool_id: str,
            _parameters: Dict[str, Any],
            **_kwargs: Any,
        ) -> ExecutionResult:
            local_calls.append(tool_id)
            raise AssertionError("denied local calls must not execute on resume")

    async def _compress_stub(**kwargs: Any) -> _StubCompressionResult:
        raw_result = dict(kwargs["raw_result"])
        status = str(raw_result.get("status") or "unknown")
        return _StubCompressionResult(
            _StubCompactResult(
                tool=str(kwargs["tool_name"]),
                summary=f"compact:{status}",
            )
        )

    state = _mixed_lane_runner_state()
    state.facts.metadata["tool_approval_gate_completed"] = True
    state.facts.metadata["tool_approval_response"] = {
        "action": "approve",
        "decisions": {
            "tc_shell": {"action": "approve"},
            "tc_backend": {"action": "skip"},
            "tc_artifact": {"action": "skip"},
        },
    }

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.GraphToolExecutor._get_executor",
        lambda self, workspace_path, task_id, model, runtime_placement_mode="local": _LocalExecutor(),
    )
    patch_tool_execution_attr(monkeypatch, "should_require_approval", lambda _metadata: True)
    patch_tool_execution_attr(
        monkeypatch,
        "request_tool_approval",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("resume with cached approval must not prompt again")
        ),
    )
    patch_tool_execution_attr(monkeypatch, "compress_tool_output", _compress_stub)
    _patch_streamless_no_provenance(monkeypatch)
    patch_tool_execution_attr(monkeypatch, "save_execution_artifact_service", lambda **_kwargs: None)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        state.as_graph_state(),
        context=GraphRuntimeContext(
            task_id=88,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-88",
            actor_type="agent",
            actor_id="langgraph",
            workspace_path=str(tmp_path),
            model="model",
        ),
    )

    assert len(provider_calls) == 1
    assert provider_calls[0].payload["tool"] == "shell.exec"
    assert provider_calls[0].payload["command_id"] == "tc_shell"
    assert local_calls == []

    updated = InteractiveState.from_mapping(result)
    rows = {
        row["tool_call_id"]: row
        for row in updated.facts.metadata["last_tool_result_compact_batch"]["results"]
    }
    assert rows["tc_shell"]["status"] == "success"
    assert rows["tc_backend"]["status"] == "denied"
    assert rows["tc_artifact"]["status"] == "denied"


@pytest.mark.asyncio
async def test_cancellation_before_dispatch_returns_local_cancelled_without_runner_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_calls: List[Any] = []

    class _Provider:
        async def send_tool_command(self, request: Any) -> Any:
            provider_calls.append(request)
            raise AssertionError("pre-dispatch cancellation must skip runner send")

    class _Registry:
        def get_provider(self, *, runtime_placement_mode: Any) -> _Provider:
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.orchestrator.build_batch_cancel_check",
        lambda **_kwargs: (lambda: True),
    )
    _patch_streamless_no_provenance(monkeypatch)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        _runner_cancelled_state().as_graph_state(),
        context=GraphRuntimeContext(
            task_id=89,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-89",
            actor_type="agent",
            actor_id="langgraph",
            workspace_path=str(tmp_path),
            model="model",
        ),
    )

    assert provider_calls == []
    updated = InteractiveState.from_mapping(result)
    rows = {
        row["tool_call_id"]: row
        for row in updated.facts.metadata["last_tool_result_compact_batch"]["results"]
    }
    assert rows["tc_shell"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_parallel_cancellation_before_dispatch_returns_local_cancelled_without_runner_send(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_calls: List[Any] = []

    class _Provider:
        async def send_tool_command(self, request: Any) -> Any:
            provider_calls.append(request)
            raise AssertionError("pre-dispatch cancellation must skip runner send")

    class _Registry:
        def get_provider(self, *, runtime_placement_mode: Any) -> _Provider:
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.orchestrator.build_batch_cancel_check",
        lambda **_kwargs: (lambda: True),
    )
    _force_parallel_validation(monkeypatch)
    _patch_streamless_no_provenance(monkeypatch)

    from agent.graph.subgraphs.tool_execution import run_tool_execution

    result = await run_tool_execution(
        _runner_parallel_cancelled_state().as_graph_state(),
        context=GraphRuntimeContext(
            task_id=90,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-90",
            actor_type="agent",
            actor_id="langgraph",
            workspace_path=str(tmp_path),
            model="model",
        ),
    )

    assert provider_calls == []
    updated = InteractiveState.from_mapping(result)
    rows = {
        row["tool_call_id"]: row
        for row in updated.facts.metadata["last_tool_result_compact_batch"]["results"]
    }
    assert rows["tc_shell_a"]["status"] == "cancelled"
    assert rows["tc_shell_b"]["status"] == "cancelled"
