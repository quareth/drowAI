"""Contracts for caller-agnostic terminal tool-execution sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import agent.graph.nodes.terminal_session_compressor as terminal_compressor_module
import agent.graph.subgraphs.shell_interaction_decision as interaction_decision_module
import agent.graph.subgraphs.tool_execution_session as session_module
import agent.graph.subgraphs.tool_execution_terminal_finalization as terminal_finalization_module
from agent.graph.nodes.terminal_session_compressor import (
    compress_terminal_execution_session_output,
)
from agent.graph.nodes.post_tool_reasoning.policies.intent_contract import (
    _extract_expected_targets,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution_session import (
    abort_execution_session_state,
    append_execution_session_evidence,
    append_shell_interaction_transcript,
    begin_execution_session_state,
    build_tool_execution_session_subgraph,
    collect_tool_execution_session_result,
    coordinate_shell_interaction,
    initialize_tool_execution_session,
    read_shell_interaction_transcript,
    route_after_tool_dispatch,
)
from agent.graph.runtime_controls import (
    read_execution_session_control,
    set_active_execution_control,
    set_execution_session_control,
)
from agent.subagents.definition import load_subagent_definitions
from agent.subagents.runtime.graph import build_subagent_state_graph
from agent.subagents.runtime.model import _build_previous_tool_context
from core.prompts.builders.post_tool.evidence import (
    EvidenceView,
    read_compact_evidence,
    register_runtime_compact_evidence,
    select_compact_evidence_for_reasoning,
)
from core.prompts.builders.post_tool.last_tool import extract_last_tool_sections
from runtime_shared.shell_session_contracts import (
    ShellInteractionBoundary,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionLifecycleStatus,
    ShellSessionStatus,
    ShellSessionUpdate,
    ShellWaitRequest,
    ShellWriteRequest,
)
from runtime_shared.shell_session_port import (
    override_shell_session_service_resolver,
)


def _shell_update(
    *,
    stdout: str = "",
    stderr: str = "",
    process_status: ShellProcessStatus,
    session_status: ShellSessionLifecycleStatus,
    session_id: str | None = "shs_1",
    interaction_boundary: ShellInteractionBoundary,
    success: bool = True,
    status: ShellSessionStatus = "success",
    exit_code: int | None = None,
    stdin_available: bool = False,
    stdout_ends_with_newline: bool = False,
    error_code: ShellSessionErrorCode | None = None,
    artifacts: list[str] | None = None,
) -> ShellSessionUpdate:
    return ShellSessionUpdate(
        success=success,
        status=status,
        process_status=process_status,
        session_status=session_status,
        interaction_boundary=interaction_boundary,
        session_id=session_id,
        stdout=stdout,
        stderr=stderr,
        artifacts=list(artifacts or []),
        exit_code=exit_code,
        stdin_available=stdin_available,
        stdout_ends_with_newline=stdout_ends_with_newline,
        truncated=False,
        duration_ms=11,
        error_code=error_code,
    )


def _row(
    *,
    call_id: str,
    tool_id: str,
    summary: str,
    originating_capability: str | None = None,
    stdout: str = "",
    stderr: str = "",
    process_status: str | None = None,
    session_id: str | None = None,
    exit_code: int | None = None,
) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "tool_call_id": call_id,
        "tool": tool_id,
        "status": "success",
        "success": True,
        "summary": summary,
    }
    if stdout:
        compact["stdout"] = stdout
    if stderr:
        compact["stderr"] = stderr
    if process_status is not None:
        compact["process_status"] = process_status
    if session_id is not None:
        compact["session_id"] = session_id
    if exit_code is not None:
        compact["exit_code"] = exit_code
    if originating_capability is not None:
        compact["metadata"] = {
            "runtime_session": {
                "originating_capability": originating_capability,
            }
        }
    return {
        "tool_call_id": call_id,
        "tool_id": tool_id,
        "intent": summary,
        "status": "success",
        "success": True,
        "compact_tool_result": compact,
    }


def _view(batch_id: str, rows: list[dict[str, Any]]) -> EvidenceView:
    return EvidenceView(
        source="batch",
        status="completed",
        success=True,
        rows=tuple(rows),
        successful_rows=tuple(rows),
        raw={
            "tool_batch_id": batch_id,
            "status": "completed",
            "success": True,
            "results": rows,
        },
    )


def _initial_shell_state(
    *,
    batch_id: str,
    call_id: str,
    command: str,
) -> InteractiveState:
    return InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run a shell command.",
            metadata={
                "turn_sequence": 7,
                "tool_batch_id": batch_id,
                "planner_plan": {
                    "tool_batch": {
                        "tool_batch_id": batch_id,
                        "requested_execution_strategy": "sequential",
                        "tool_calls": [
                            {
                                "tool_call_id": call_id,
                                "tool_id": "shell.utility",
                                "parameters": {"command": command},
                                "intent": "Run the requested command.",
                            }
                        ],
                    }
                },
            },
        ),
        trace=TraceState(),
    )


def _add_shell_identity(metadata: dict[str, Any], *, task_id: int = 42) -> None:
    metadata.update(
        {
            "tenant_id": 1,
            "task_id": task_id,
            "execution_owner_id": f"main:task-{task_id}-turn-1",
            "runtime_placement_mode": "local",
            "workspace_id": f"task-{task_id}",
            "workspace_path": "/workspace",
        }
    )


def test_completed_ordinary_batch_bypasses_interactive_session_unchanged() -> None:
    """Ordinary multi-tool evidence never enters transcript aggregation."""

    rows = [
        _row(
            call_id="call-alpha",
            tool_id="tool.alpha",
            summary="Alpha completed.",
            stdout="alpha-out\n",
        ),
        _row(
            call_id="call-beta",
            tool_id="tool.beta",
            summary="Beta completed.",
            stdout="beta-out\n",
        ),
    ]
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run two ordinary tools.",
            metadata={
                "turn_sequence": 7,
                "tool_batch_id": "batch-ordinary",
                "planner_plan": {
                    "tool_batch": {
                        "tool_batch_id": "batch-ordinary",
                        "tool_calls": [
                            {
                                "tool_call_id": "call-alpha",
                                "tool_id": "tool.alpha",
                                "parameters": {},
                            },
                            {
                                "tool_call_id": "call-beta",
                                "tool_id": "tool.beta",
                                "parameters": {},
                            },
                        ],
                    }
                },
                "last_tool_result_compact_batch": dict(
                    _view("batch-ordinary", rows).raw
                ),
                "last_tool_result_compact": dict(rows[-1]["compact_tool_result"]),
            },
        ),
        trace=TraceState(),
    )

    assert route_after_tool_dispatch(state) == "terminal"
    initialized = initialize_tool_execution_session(state)
    collected = collect_tool_execution_session_result(initialized)
    metadata = collected["facts"]["metadata"]

    assert read_execution_session_control(metadata) is None
    assert read_shell_interaction_transcript("batch-ordinary") is None
    assert metadata["last_tool_result_compact_batch"]["results"] == rows
    assert metadata["last_tool_result_compact"]["stdout"] == "beta-out\n"
    assert (
        "execution_session_aggregate"
        not in metadata["last_tool_result_compact_batch"]
    )


def test_session_initialization_uses_the_active_non_primary_shell_call() -> None:
    sequence_id = "batch-active-non-primary"
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run assessment and utility commands.",
            metadata={
                "turn_sequence": 7,
                "tool_batch_id": sequence_id,
                "planner_plan": {
                    "tool_batch": {
                        "tool_batch_id": sequence_id,
                        "requested_execution_strategy": "sequential",
                        "tool_calls": [
                            {
                                "tool_call_id": "call-assessment",
                                "tool_id": "information_gathering.network_discovery.nmap",
                                "parameters": {"target": "localhost"},
                                "intent": "Collect assessment evidence.",
                            },
                            {
                                "tool_call_id": "call-utility",
                                "tool_id": "shell.utility",
                                "parameters": {"command": "sleep 30"},
                                "intent": "Wait for a utility process.",
                            },
                        ],
                    }
                },
            },
        ),
        trace=TraceState(),
    )
    metadata = state.facts.ensure_metadata()
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "originating_tool_call_id": "call-utility",
            "originating_tool_batch_id": sequence_id,
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_active_utility",
            "stdin_available": True,
        },
    )

    initialized = initialize_tool_execution_session(state)
    initialized_metadata = initialized["facts"]["metadata"]
    session = read_execution_session_control(initialized_metadata)
    transcript = read_shell_interaction_transcript(sequence_id)
    try:
        assert session is not None
        assert session["originating_tool_id"] == "shell.utility"
        assert session["originating_tool_call_id"] == "call-utility"
        assert session["originating_tool_batch_id"] == sequence_id
        assert transcript is not None
        assert transcript["originating_tool_id"] == "shell.utility"
        assert transcript["originating_command"] == "sleep 30"
    finally:
        abort_execution_session_state(sequence_id)


def test_running_dispatch_routes_to_interactive_session() -> None:
    state = _initial_shell_state(
        batch_id="batch-running-route",
        call_id="call-running-route",
        command="python3 -u prompt.py",
    )
    metadata = state.facts.ensure_metadata()
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_running_route",
        },
    )
    state.facts.metadata = metadata

    assert route_after_tool_dispatch(state) == "execution_session"


def test_subagent_shell_identity_ignores_runner_artifact_workspace_path() -> None:
    interactive = InteractiveState(
        facts=FactsState(
            task_id=49,
            message="Wait for the running shell command.",
            metadata={},
        ),
        trace=TraceState(),
    )
    metadata = {
        "tenant_id": 1,
        "task_id": 49,
        "execution_owner_id": "subagent:agent-run-49",
        "runtime_placement_mode": "runner",
        "workspace_id": "task-49",
        "workspace_path": "/host/artifacts/task-49",
        "runner_id": "runner-1",
        "execution_site_id": "site-1",
    }
    context = SimpleNamespace(
        tenant_id=1,
        task_id=49,
        execution_owner_id="subagent:agent-run-49",
        runtime_placement_mode="runner",
        workspace_id="task-49",
        workspace_path=None,
        runner_id="runner-1",
        execution_site_id="site-1",
    )

    identity = session_module._shell_session_identity_from_context(
        interactive=interactive,
        metadata=metadata,
        context=context,
    )

    assert identity == ShellSessionIdentity(
        tenant_id=1,
        task_id=49,
        execution_owner_id="subagent:agent-run-49",
        runtime_placement_mode="runner",
        workspace_id="task-49",
        workspace_path=None,
        runner_id="runner-1",
        execution_site_id="site-1",
    )


def _terminal_state(
    *,
    final_batch_id: str,
    sequence_id: str,
    originating_tool_id: str,
) -> InteractiveState:
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run an interactive command with shell.utility.",
            metadata={
                "turn_sequence": 7,
                "tool_batch_id": final_batch_id,
            },
        ),
        trace=TraceState(),
    )
    set_execution_session_control(
        state.facts.ensure_metadata(),
        turn_sequence=7,
        sequence_id=sequence_id,
        originating_tool_id=originating_tool_id,
    )
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id=originating_tool_id,
        originating_parameters={"command": "python3 -u interactive.py"},
    )
    return state


def test_terminal_utility_session_retains_runtime_only_aggregate() -> None:
    sequence_id = "batch-start-utility"
    final_batch_id = "batch-finish-utility"
    start_row = _row(
        call_id="call-start",
        tool_id="shell.utility",
        summary="Started session shs_1.",
    )
    finish_row = _row(
        call_id="call-finish",
        tool_id="shell.write_stdin",
        summary="Command completed.",
        originating_capability="utility",
    )
    register_runtime_compact_evidence(
        _view(final_batch_id, [finish_row]).raw,
        single_compact=finish_row["compact_tool_result"],
    )
    state = _terminal_state(
        final_batch_id=final_batch_id,
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
    )
    append_execution_session_evidence(sequence_id, _view(sequence_id, [start_row]))

    updated = collect_tool_execution_session_result(state)
    metadata = updated["facts"]["metadata"]
    runtime = read_compact_evidence(metadata, prefer_runtime=True)

    assert runtime is not None
    assert [row["tool_id"] for row in runtime.rows] == [
        "shell.utility",
        "shell.write_stdin",
    ]
    assert runtime.raw["execution_session_aggregate"] is True
    assert "last_tool_result_compact_batch" not in metadata
    assert "last_tool_result_compact" not in metadata
    assert read_execution_session_control(metadata) is None
    prompt_context = _build_previous_tool_context(
        InteractiveState.from_mapping(updated)
    )
    assert [
        row["tool_id"]
        for row in prompt_context["last_tool_result"]["results"]
    ] == ["shell.utility", "shell.write_stdin"]


def test_terminal_assessment_session_retains_its_aggregate_durably() -> None:
    sequence_id = "batch-start-assessment"
    final_batch_id = "batch-finish-assessment"
    start_row = _row(
        call_id="call-start",
        tool_id="shell.assessment",
        summary="Started assessment session shs_2.",
    )
    finish_row = _row(
        call_id="call-finish",
        tool_id="shell.write_stdin",
        summary="Assessment completed.",
        originating_capability="assessment",
    )
    artifact_path = "artifacts/shell-assessment-shs_2.txt"
    finish_row["compact_tool_result"]["artifacts"] = [artifact_path]
    register_runtime_compact_evidence(
        _view(final_batch_id, [finish_row]).raw,
        single_compact=finish_row["compact_tool_result"],
    )
    state = _terminal_state(
        final_batch_id=final_batch_id,
        sequence_id=sequence_id,
        originating_tool_id="shell.assessment",
    )
    append_execution_session_evidence(sequence_id, _view(sequence_id, [start_row]))

    updated = collect_tool_execution_session_result(state)
    durable = updated["facts"]["metadata"]["last_tool_result_compact_batch"]

    assert [row["tool_id"] for row in durable["results"]] == [
        "shell.assessment",
        "shell.write_stdin",
    ]
    assert durable["execution_session_aggregate"] is True
    assert durable["results"][-1]["compact_tool_result"]["artifact_refs"] == [
        {"path": artifact_path, "count": 1}
    ]
    ptr_sections = extract_last_tool_sections(
        updated["facts"]["metadata"],
        updated["facts"],
    )
    assert artifact_path in ptr_sections["artifact_refs"]


def test_terminal_assessment_finalizes_existing_provenance_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id = "batch-assessment-provenance"
    final_batch_id = "batch-assessment-provenance-finish"
    start_row = _row(
        call_id="call-assessment-origin",
        tool_id="shell.assessment",
        summary="Assessment started.",
        stdout="banner\n",
        process_status="running",
        session_id="shs_assessment_provenance",
    )
    artifact_path = "artifacts/shell-assessment-shs_assessment_provenance.txt"
    finish_row = _row(
        call_id="call-assessment-finish",
        tool_id="shell.write_stdin",
        summary="Assessment completed.",
        originating_capability="assessment",
        stdout="response body\n",
        process_status="completed",
        exit_code=0,
    )
    finish_row["compact_tool_result"]["artifacts"] = [artifact_path]
    register_runtime_compact_evidence(
        _view(final_batch_id, [finish_row]).raw,
        single_compact=finish_row["compact_tool_result"],
    )
    state = _terminal_state(
        final_batch_id=final_batch_id,
        sequence_id=sequence_id,
        originating_tool_id="shell.assessment",
    )
    set_execution_session_control(
        state.facts.ensure_metadata(),
        turn_sequence=7,
        sequence_id=sequence_id,
        originating_tool_id="shell.assessment",
        originating_tool_call_id="call-assessment-origin",
        originating_tool_batch_id=sequence_id,
        provenance_execution_id="execution-assessment-1",
    )
    append_execution_session_evidence(sequence_id, _view(sequence_id, [start_row]))
    append_shell_interaction_transcript(
        sequence_id=sequence_id,
        evidence=_view(sequence_id, [start_row]),
        metadata=state.facts.metadata,
    )
    finalizations: list[dict[str, Any]] = []
    emitted_events: list[dict[str, Any]] = []

    def _finalize(**kwargs: Any) -> list[dict[str, Any]]:
        finalizations.append(dict(kwargs))
        return [
            {
                "artifact_id": "artifact-assessment-1",
                "execution_id": "execution-assessment-1",
                "tool_call_id": "call-assessment-origin",
                "tool_name": "shell.assessment",
                "artifact_kind": "tool_file",
                "path": artifact_path,
                "relative_path": artifact_path,
                "label": "assessment output",
            }
        ]

    monkeypatch.setattr(
        terminal_finalization_module,
        "finalize_provenance_execution",
        _finalize,
    )
    monkeypatch.setattr(
        session_module,
        "get_stream_writer",
        lambda: emitted_events.append,
    )

    updated = collect_tool_execution_session_result(state)
    terminal = updated["facts"]["metadata"]["last_tool_result_compact"]

    assert len(finalizations) == 1
    assert finalizations[0]["execution_id"] == "execution-assessment-1"
    assert finalizations[0]["tool_call_id"] == "call-assessment-origin"
    assert finalizations[0]["outcome"].result["stdout"] == (
        "banner\nresponse body\n"
    )
    assert terminal["artifact_refs"][0]["artifact_id"] == (
        "artifact-assessment-1"
    )
    assert len(emitted_events) == 1
    assert emitted_events[0]["type"] == "tool_end"
    assert emitted_events[0]["output_persistence"] == "durable"
    assert emitted_events[0]["content"] == "banner\nresponse body\n"
    assert emitted_events[0]["compact_tool_result"]["artifact_refs"][0][
        "artifact_id"
    ] == "artifact-assessment-1"


def test_terminal_shell_lifecycle_is_durable_only_for_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(session_module, "get_stream_writer", lambda: emitted.append)
    compact = {
        "process_status": "completed",
        "session_status": "closed",
        "interaction_boundary": "terminal",
        "summary": "Command completed.",
        "stdout": "done\n",
        "exit_code": 0,
    }

    session_module._emit_shell_lifecycle_progress(
        {"turn_sequence": 7},
        tool_id="shell.assessment",
        tool_call_id="call-assessment",
        tool_batch_id="batch-assessment",
        compact=compact,
        success=True,
    )
    session_module._emit_shell_lifecycle_progress(
        {"turn_sequence": 7},
        tool_id="shell.utility",
        tool_call_id="call-utility",
        tool_batch_id="batch-utility",
        compact=compact,
        success=True,
    )

    assert [event["output_persistence"] for event in emitted] == [
        "durable",
        "transient",
    ]


def test_mixed_session_persists_only_non_utility_rows() -> None:
    sequence_id = "batch-start-mixed"
    final_batch_id = "batch-finish-mixed"
    utility_row = _row(
        call_id="call-utility",
        tool_id="shell.utility",
        summary="Inspected a local file.",
    )
    durable_row = _row(
        call_id="call-nmap",
        tool_id="information_gathering.network_discovery.nmap",
        summary="Found tcp/80 open.",
    )
    register_runtime_compact_evidence(
        _view(final_batch_id, [durable_row]).raw,
        single_compact=durable_row["compact_tool_result"],
    )
    state = _terminal_state(
        final_batch_id=final_batch_id,
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
    )
    append_execution_session_evidence(
        sequence_id,
        _view(sequence_id, [utility_row]),
    )

    updated = collect_tool_execution_session_result(state)
    metadata = updated["facts"]["metadata"]
    selected, selected_is_durable = select_compact_evidence_for_reasoning(metadata)

    assert selected is not None
    assert [row["tool_id"] for row in selected.rows] == [
        "shell.utility",
        "information_gathering.network_discovery.nmap",
    ]
    assert selected_is_durable is False
    assert [
        row["tool_id"]
        for row in metadata["last_tool_result_compact_batch"]["results"]
    ] == ["information_gathering.network_discovery.nmap"]


def test_lost_process_local_session_fails_closed_as_unavailable() -> None:
    sequence_id = "batch-lost-transcript"
    final_batch_id = "batch-lost-transcript-finish"
    row = _row(
        call_id="call-finish",
        tool_id="shell.write_stdin",
        summary="Command completed.",
        originating_capability="utility",
        stdout="done\n",
        process_status="completed",
        session_id="shs_lost_transcript",
        exit_code=0,
    )
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "python3 -u interactive.py"},
    )
    abort_execution_session_state(sequence_id)
    register_runtime_compact_evidence(
        _view(final_batch_id, [row]).raw,
        single_compact=row["compact_tool_result"],
    )
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Continue a shell session after restart.",
            metadata={
                "turn_sequence": 7,
                "tool_batch_id": final_batch_id,
            },
        ),
        trace=TraceState(),
    )
    set_execution_session_control(
        state.facts.ensure_metadata(),
        turn_sequence=7,
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
    )

    updated = collect_tool_execution_session_result(state)
    metadata = updated["facts"]["metadata"]
    unavailable = metadata["last_tool_result_compact_batch"]["results"][0]

    assert unavailable["success"] is False
    assert unavailable["failure_category"] == "tool_unavailable"
    assert unavailable["compact_tool_result"]["session_status"] == "unavailable"
    assert unavailable["compact_tool_result"]["process_status"] == "failed"
    assert unavailable["compact_tool_result"]["stdout"] == ""
    assert read_execution_session_control(metadata) is None


def test_abort_discards_transcript_and_evidence_together() -> None:
    sequence_id = "batch-aborted-session"
    row = _row(
        call_id="call-start",
        tool_id="shell.utility",
        summary="Session started.",
    )
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "sleep 10"},
    )
    append_execution_session_evidence(sequence_id, _view(sequence_id, [row]))

    abort_execution_session_state(sequence_id)

    assert read_shell_interaction_transcript(sequence_id) is None
    with pytest.raises(KeyError, match="Unknown runtime execution session"):
        append_execution_session_evidence(sequence_id, _view(sequence_id, [row]))


def test_active_execution_sessions_are_not_silently_evicted() -> None:
    sequence_ids = [f"session-{index}" for index in range(129)]
    try:
        for sequence_id in sequence_ids:
            begin_execution_session_state(
                sequence_id=sequence_id,
                originating_tool_id="shell.utility",
                originating_parameters={"command": "sleep 20"},
            )

        assert read_shell_interaction_transcript(sequence_ids[0]) is not None
        assert read_shell_interaction_transcript(sequence_ids[-1]) is not None
    finally:
        for sequence_id in sequence_ids:
            abort_execution_session_state(sequence_id)


def test_legacy_shell_exec_continuation_keeps_assessment_provenance() -> None:
    compact = session_module._compact_from_shell_update(
        tool_id="shell.write_stdin",
        update=_shell_update(
            stdout="done\n",
            process_status=ShellProcessStatus.COMPLETED,
            session_status=ShellSessionLifecycleStatus.CLOSED,
            session_id=None,
            interaction_boundary=ShellInteractionBoundary.TERMINAL,
            exit_code=0,
        ),
        originating_tool_id="shell.exec",
    )

    assert compact["metadata"]["runtime_session"]["originating_capability"] == (
        "assessment"
    )


def test_direct_assessment_continuation_preserves_runtime_artifact_reference() -> None:
    artifact_path = "artifacts/shell-assessment-shs_verified.txt"
    compact = session_module._compact_from_shell_update(
        tool_id="shell.write_stdin",
        update=_shell_update(
            process_status=ShellProcessStatus.COMPLETED,
            session_status=ShellSessionLifecycleStatus.CLOSED,
            session_id=None,
            interaction_boundary=ShellInteractionBoundary.TERMINAL,
            exit_code=0,
            artifacts=[artifact_path],
        ),
        originating_tool_id="shell.assessment",
    )

    assert compact["artifacts"] == [artifact_path]
    assert compact["metadata"]["artifact_scope"] == "runtime_workspace"
    assert compact["metadata"]["runtime_session"]["artifact_capture"] == {
        "status": "succeeded",
        "artifact_count": 1,
    }


def test_direct_utility_continuation_never_projects_artifacts() -> None:
    compact = session_module._compact_from_shell_update(
        tool_id="shell.write_stdin",
        update=_shell_update(
            process_status=ShellProcessStatus.COMPLETED,
            session_status=ShellSessionLifecycleStatus.CLOSED,
            session_id=None,
            interaction_boundary=ShellInteractionBoundary.TERMINAL,
            exit_code=0,
            artifacts=["artifacts/unexpected.txt"],
        ),
        originating_tool_id="shell.utility",
    )

    assert compact["artifacts"] == []
    assert "artifact_scope" not in compact["metadata"]
    assert "artifact_capture" not in compact["metadata"]["runtime_session"]


@pytest.mark.asyncio
async def test_interaction_failure_preserves_original_error_and_aborts_session() -> None:
    sequence_id = "batch-interaction-failure"
    state = _initial_shell_state(
        batch_id=sequence_id,
        call_id="call-interaction-failure",
        command="sleep 30",
    )
    metadata = state.facts.ensure_metadata()
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_interaction_failure",
            "stdin_available": True,
        },
    )
    initialized = initialize_tool_execution_session(state)

    async def _raise_interaction(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("sentinel interaction failure")

    graph = build_tool_execution_session_subgraph(
        initialize_fn=lambda current, context=None: current,
        collect_fn=lambda current, context=None: current,
        interaction_fn=_raise_interaction,
    )

    with pytest.raises(RuntimeError, match="sentinel interaction failure"):
        await graph.ainvoke(initialized)

    assert read_shell_interaction_transcript(sequence_id) is None


@pytest.mark.asyncio
async def test_default_interaction_boundary_uses_model_structured_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id = "batch-model-decision"
    ready_row = _row(
        call_id="call-start",
        tool_id="shell.utility",
        summary="Session is running.",
        stdout="READY\n",
        process_status="running",
        session_id="shs_model_decision",
    )
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Send hello when the program is ready.",
            current_goal="Respond to READY with hello.",
            tool_ids=["shell.utility"],
            tool_candidates=["shell.utility"],
            metadata={
                "turn_sequence": 7,
                "tool_batch_id": sequence_id,
            },
        ),
        trace=TraceState(),
    )
    metadata = state.facts.ensure_metadata()
    _add_shell_identity(metadata)
    set_execution_session_control(
        metadata,
        turn_sequence=7,
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
    )
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_model_decision",
            "stdin_available": True,
        },
    )
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={
            "command": "python3 -u prompt.py",
            "interactive": True,
        },
    )
    append_shell_interaction_transcript(
        sequence_id=sequence_id,
        evidence=_view(sequence_id, [ready_row]),
        metadata=metadata,
    )
    prompts: list[dict[str, Any]] = []
    write_calls: list[tuple[ShellSessionIdentity, ShellWriteRequest]] = []

    class _DecisionLLM:
        async def chat_with_usage(
            self,
            system_prompt: str,
            user_prompt: str,
            **kwargs: Any,
        ) -> Any:
            prompts.append(
                {
                    "system": system_prompt,
                    "user": json.loads(user_prompt),
                    "structured_output": kwargs.get("structured_output"),
                }
            )
            return SimpleNamespace(
                structured_output={
                    "action": "send_input",
                    "chars": "hello\n",
                    "reasoning": "READY asks for the configured input.",
                }
            )

    monkeypatch.setattr(
        interaction_decision_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _DecisionLLM(),
    )

    class _ShellService:
        async def write_stdin(
            self,
            *,
            identity: ShellSessionIdentity,
            request: ShellWriteRequest,
        ) -> ShellSessionUpdate:
            write_calls.append((identity, request))
            return _shell_update(
                stdout="ACK\n",
                process_status=ShellProcessStatus.RUNNING,
                session_status=ShellSessionLifecycleStatus.ACTIVE,
                session_id=request.session_id,
                interaction_boundary=ShellInteractionBoundary.OUTPUT_AVAILABLE,
                stdin_available=True,
            )

        async def wait_for_output(
            self,
            *,
            identity: ShellSessionIdentity,
            request: ShellWaitRequest,
        ) -> ShellSessionUpdate:
            raise AssertionError("send_input must not poll via wait_for_output")

    with override_shell_session_service_resolver(lambda: _ShellService()):
        updated = await coordinate_shell_interaction(
            state,
            config={"configurable": {}},
        )
    updated_metadata = updated["facts"]["metadata"]
    row = updated_metadata["last_tool_result_compact_batch"]["results"][0]

    assert prompts[0]["user"]["transcript"]["originating_command"] == (
        "python3 -u prompt.py"
    )
    assert prompts[0]["user"]["transcript"]["entries"][0]["stdout"] == "READY\n"
    assert prompts[0]["structured_output"].name == "shell_interaction_decision"
    assert write_calls == [
        (
            ShellSessionIdentity(
                tenant_id=1,
                task_id=42,
                execution_owner_id="main:task-42-turn-1",
                runtime_placement_mode="local",
                workspace_id="task-42",
                workspace_path="/workspace",
                runner_id=None,
                execution_site_id=None,
            ),
            ShellWriteRequest(session_id="shs_model_decision", chars="hello\n"),
        )
    ]
    assert row["tool_id"] == "shell.write_stdin"
    assert row["compact_tool_result"]["stdout"] == "ACK\n"
    assert "planner_plan" not in updated_metadata
    assert "tool_plan_prepared" not in updated_metadata


@pytest.mark.asyncio
async def test_decision_failure_interrupts_instead_of_waiting_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id = "batch-decision-failure"
    state = _initial_shell_state(
        batch_id=sequence_id,
        call_id="call-decision-failure",
        command="bc",
    )
    metadata = state.facts.ensure_metadata()
    _add_shell_identity(metadata)
    set_execution_session_control(
        metadata,
        turn_sequence=7,
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
    )
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_decision_failure",
            "stdin_available": True,
        },
    )
    begin_execution_session_state(
        sequence_id=sequence_id,
        originating_tool_id="shell.utility",
        originating_parameters={"command": "bc", "interactive": True},
    )
    append_shell_interaction_transcript(
        sequence_id=sequence_id,
        evidence=_view(
            sequence_id,
            [
                _row(
                    call_id="call-decision-failure",
                    tool_id="shell.utility",
                    summary="Session is running quietly.",
                    process_status="running",
                    session_id="shs_decision_failure",
                )
            ],
        ),
        metadata=metadata,
    )

    class _FailingDecisionLLM:
        async def chat_with_usage(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("decision service unavailable")

    monkeypatch.setattr(
        interaction_decision_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: _FailingDecisionLLM(),
    )
    write_calls: list[ShellWriteRequest] = []

    class _ShellService:
        async def write_stdin(
            self,
            *,
            identity: ShellSessionIdentity,
            request: ShellWriteRequest,
        ) -> ShellSessionUpdate:
            _ = identity
            write_calls.append(request)
            return _shell_update(
                process_status=ShellProcessStatus.TERMINATED,
                session_status=ShellSessionLifecycleStatus.CLOSED,
                session_id=None,
                interaction_boundary=ShellInteractionBoundary.TERMINAL,
                exit_code=130,
            )

        async def wait_for_output(
            self,
            *,
            identity: ShellSessionIdentity,
            request: ShellWaitRequest,
        ) -> ShellSessionUpdate:
            _ = (identity, request)
            raise AssertionError("decision failure must not become an unbounded wait")

    with override_shell_session_service_resolver(lambda: _ShellService()):
        updated = await coordinate_shell_interaction(
            state,
            config={"configurable": {}},
        )

    updated_metadata = updated["facts"]["metadata"]
    compact = updated_metadata["last_tool_result_compact"]
    assert write_calls == [
        ShellWriteRequest(session_id="shs_decision_failure", chars="\u0003")
    ]
    assert compact["process_status"] == "terminated"
    assert compact["summary"] == (
        "Interactive session coordination failed; an interrupt was sent to avoid "
        "leaving the command stuck."
    )


@pytest.mark.asyncio
async def test_coordinator_uses_bounded_runtime_wait() -> None:
    state = _initial_shell_state(
        batch_id="batch-bounded-wait",
        call_id="call-bounded-wait",
        command="bc -q",
    )
    metadata = state.facts.ensure_metadata()
    _add_shell_identity(metadata)
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "originating_tool_call_id": "call-bounded-wait",
            "originating_tool_batch_id": "batch-bounded-wait",
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_bounded_wait",
            "stdin_available": True,
        },
    )
    initialized = initialize_tool_execution_session(state)
    interactive = InteractiveState.from_mapping(initialized)
    metadata = interactive.facts.ensure_metadata()
    interactive.facts.metadata = metadata
    wait_requests: list[ShellWaitRequest] = []

    class _ShellService:
        async def wait_for_output(
            self,
            *,
            identity: ShellSessionIdentity,
            request: ShellWaitRequest,
        ) -> ShellSessionUpdate:
            _ = identity
            wait_requests.append(request)
            return _shell_update(
                process_status=ShellProcessStatus.RUNNING,
                session_status=ShellSessionLifecycleStatus.ACTIVE,
                session_id="shs_bounded_wait",
                interaction_boundary=ShellInteractionBoundary.QUIET_BOUNDARY,
                stdin_available=True,
            )

    with override_shell_session_service_resolver(lambda: _ShellService()):
        updated = await coordinate_shell_interaction(
            interactive,
            decide_fn=lambda **_kwargs: {"action": "wait_for_output"},
        )

    assert wait_requests == [ShellWaitRequest(session_id="shs_bounded_wait")]
    updated_metadata = updated["facts"]["metadata"]
    assert updated_metadata["tool_batch_id"].startswith("tb_")
    runtime_batch = updated_metadata["last_tool_result_compact_batch"]
    assert runtime_batch["results"][0]["tool_call_id"].startswith("tc_")


@pytest.mark.asyncio
async def test_quiet_wait_emits_visible_progress_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted_events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_module, "get_stream_writer", lambda: emitted_events.append)
    state = _initial_shell_state(
        batch_id="batch-visible-wait",
        call_id="call-visible-wait",
        command="sleep 30",
    )
    metadata = state.facts.ensure_metadata()
    _add_shell_identity(metadata)
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "originating_tool_call_id": "call-visible-wait",
            "originating_tool_batch_id": "batch-visible-wait",
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_visible_wait",
            "stdin_available": True,
        },
    )
    initialized = initialize_tool_execution_session(state)
    interactive = InteractiveState.from_mapping(initialized)
    metadata = interactive.facts.ensure_metadata()
    interactive.facts.metadata = metadata

    async def wait_fn(**_kwargs: Any) -> ShellSessionUpdate:
        return _shell_update(
            process_status=ShellProcessStatus.RUNNING,
            session_status=ShellSessionLifecycleStatus.ACTIVE,
            session_id="shs_visible_wait",
            interaction_boundary=ShellInteractionBoundary.QUIET_BOUNDARY,
            stdin_available=True,
        )

    await coordinate_shell_interaction(
        interactive,
        decide_fn=lambda **_kwargs: {"action": "wait_for_output"},
        wait_fn=wait_fn,
    )

    assert emitted_events[0]["type"] == "tool_delta"
    assert emitted_events[0]["content"] == (
        "Command is still running and accepts additional input; "
        "no new output was produced."
    )


def test_subagent_uses_shared_session_then_returns_directly_to_its_model_loop() -> None:
    definition = next(
        item for item in load_subagent_definitions() if item.id == "pathfinder"
    )
    graph = build_subagent_state_graph(definition)

    assert "tool_execution_session" in graph.nodes
    assert "approval_gate" in graph.nodes
    assert "dispatch_tool" in graph.nodes
    assert "terminal_session_compressor" in graph.nodes
    assert "tool_synthesizer" in graph.nodes
    assert (
        "tool_execution_session",
        "terminal_session_compressor",
    ) in graph.edges
    assert (
        "terminal_session_compressor",
        "tool_synthesizer",
    ) in graph.edges
    assert ("approval_gate", "dispatch_tool") in graph.edges
    assert ("tool_synthesizer", "observation") in graph.edges
    assert ("observation", "model") in graph.edges


def test_registered_dotted_tool_id_is_not_misclassified_as_a_target() -> None:
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run an interactive command with shell.utility.",
        ),
        trace=TraceState(),
    )

    assert _extract_expected_targets(state) == []


@pytest.mark.asyncio
async def test_running_command_continuation_completes_inside_one_subgraph() -> None:
    dispatch_calls: list[str] = []
    write_calls: list[ShellWriteRequest] = []
    decision_transcripts: list[dict[str, Any]] = []

    async def dispatch(state: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        turn_sequence = metadata["turn_sequence"]
        dispatch_calls.append("shell.utility")
        row = _row(
            call_id="call-start",
            tool_id="shell.utility",
            summary="Session is running.",
            stdout="READY\n",
            process_status="running",
            session_id="shs_1",
        )
        register_runtime_compact_evidence(
            _view("batch-start", [row]).raw,
            single_compact=row["compact_tool_result"],
        )
        set_active_execution_control(
            metadata,
            turn_sequence=turn_sequence,
            active_execution={
                "originating_tool_id": "shell.utility",
                "continuation_tool_id": "shell.write_stdin",
                "process_status": "running",
                "session_id": "shs_1",
                "stdin_available": True,
            },
        )
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()

    def decide(**kwargs: Any) -> dict[str, Any]:
        decision_transcripts.append(dict(kwargs["transcript"] or {}))
        return {"action": "send_input", "chars": "hello\n"}

    class _ShellService:
        async def write_stdin(
            self,
            *,
            identity: ShellSessionIdentity,
            request: ShellWriteRequest,
        ) -> ShellSessionUpdate:
            _ = identity
            write_calls.append(request)
            return _shell_update(
                stdout="RECEIVED=hello\n",
                process_status=ShellProcessStatus.COMPLETED,
                session_status=ShellSessionLifecycleStatus.CLOSED,
                session_id=None,
                interaction_boundary=ShellInteractionBoundary.TERMINAL,
                exit_code=0,
            )

    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run a delayed interactive command.",
            metadata={
                "turn_sequence": 7,
                "tool_batch_id": "batch-start",
                "planner_plan": {
                    "tool_batch": {
                        "tool_batch_id": "batch-start",
                        "requested_execution_strategy": "sequential",
                        "tool_calls": [
                            {
                                "tool_call_id": "call-start",
                                "tool_id": "shell.utility",
                                "parameters": {
                                    "command": "sleep 11",
                                    "interactive": True,
                                },
                                "intent": "Run the requested command.",
                            }
                        ],
                    }
                },
            },
        ),
        trace=TraceState(),
    )
    _add_shell_identity(state.facts.ensure_metadata())
    dispatched = await dispatch(state.model_dump(mode="json"))
    graph = build_tool_execution_session_subgraph(
        decide_interaction_fn=decide,
    )

    with override_shell_session_service_resolver(lambda: _ShellService()):
        result = await graph.ainvoke(dispatched)
    runtime = read_compact_evidence(result["facts"]["metadata"], prefer_runtime=True)

    assert dispatch_calls == ["shell.utility"]
    assert write_calls == [ShellWriteRequest(session_id="shs_1", chars="hello\n")]
    assert runtime is not None
    assert [row["tool_id"] for row in runtime.rows] == [
        "shell.utility",
        "shell.write_stdin",
    ]
    assert decision_transcripts[0]["originating_command"] == "sleep 11"
    assert decision_transcripts[0]["entries"][0]["stdout"] == "READY\n"
    assert decision_transcripts[0]["entries"][0]["process_status"] == "running"
    assert runtime.rows[-1]["compact_tool_result"]["stdout"] == (
        "READY\nRECEIVED=hello\n"
    )
    assert read_shell_interaction_transcript("batch-start") is None
    assert read_execution_session_control(result["facts"]["metadata"]) is None


@pytest.mark.asyncio
async def test_lifecycle_progress_uses_immutable_origin_after_plan_mutates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted_events: list[dict[str, Any]] = []
    state = _initial_shell_state(
        batch_id="batch-fresh-dispatch",
        call_id="call-origin",
        command="python3 -u prompt.py",
    )
    metadata = state.facts.ensure_metadata()
    metadata["tool_batch_id"] = "batch-stale-same-call"
    _add_shell_identity(metadata)
    set_active_execution_control(
        metadata,
        turn_sequence=7,
        active_execution={
            "originating_tool_id": "shell.utility",
            "originating_tool_call_id": "call-origin",
            "originating_tool_batch_id": "batch-fresh-dispatch",
            "continuation_tool_id": "shell.write_stdin",
            "process_status": "running",
            "session_id": "shs_origin",
            "stdin_available": True,
        },
    )
    initialized = initialize_tool_execution_session(state)
    interactive = InteractiveState.from_mapping(initialized)
    metadata = interactive.facts.ensure_metadata()
    session = read_execution_session_control(metadata)
    assert session is not None
    assert session["sequence_id"] == "batch-fresh-dispatch"
    assert session["originating_tool_batch_id"] == "batch-fresh-dispatch"
    metadata["tool_batch_id"] = "batch-continuation"
    metadata["planner_plan"] = {
        "tool_batch": {
            "tool_batch_id": "batch-continuation",
            "tool_calls": [
                {
                    "tool_call_id": "call-continuation",
                    "tool_id": "shell.write_stdin",
                    "parameters": {"session_id": "shs_origin", "chars": "next\n"},
                }
            ],
        }
    }
    interactive.facts.metadata = metadata

    updates = iter(
        [
            _shell_update(
                stdout="chunk-1\n",
                process_status=ShellProcessStatus.RUNNING,
                session_status=ShellSessionLifecycleStatus.ACTIVE,
                session_id="shs_origin",
                interaction_boundary=ShellInteractionBoundary.OUTPUT_AVAILABLE,
                stdin_available=True,
                stdout_ends_with_newline=True,
            ),
            _shell_update(
                stdout="done\n",
                process_status=ShellProcessStatus.COMPLETED,
                session_status=ShellSessionLifecycleStatus.CLOSED,
                session_id=None,
                interaction_boundary=ShellInteractionBoundary.TERMINAL,
                exit_code=0,
                stdout_ends_with_newline=True,
            ),
        ]
    )

    monkeypatch.setattr(session_module, "get_stream_writer", lambda: emitted_events.append)

    async def wait_fn(**_kwargs: Any) -> ShellSessionUpdate:
        return next(updates)

    first = await coordinate_shell_interaction(
        interactive,
        decide_fn=lambda **_kwargs: {"action": "wait_for_output"},
        wait_fn=wait_fn,
    )
    second = await coordinate_shell_interaction(
        InteractiveState.from_mapping(first),
        decide_fn=lambda **_kwargs: {"action": "wait_for_output"},
        wait_fn=wait_fn,
    )

    assert [event["type"] for event in emitted_events] == ["tool_delta", "tool_end"]
    assert [event["shell_output_chunk"] for event in emitted_events] == [True, True]
    assert [event["stdout_ends_with_newline"] for event in emitted_events] == [
        True,
        True,
    ]
    assert {event["tool_call_id"] for event in emitted_events} == {"call-origin"}
    assert {event["tool_batch_id"] for event in emitted_events} == {
        "batch-fresh-dispatch"
    }
    assert "batch-stale-same-call" not in {
        event["tool_batch_id"] for event in emitted_events
    }
    assert read_execution_session_control(second["facts"]["metadata"]) is not None


@pytest.mark.asyncio
async def test_quiet_ordinary_command_waits_inside_subgraph_until_terminal() -> None:
    dispatch_calls: list[str] = []
    wait_calls = 0

    async def dispatch(state: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        dispatch_calls.append("shell.utility")
        row = _row(
            call_id="call-quiet-start",
            tool_id="shell.utility",
            summary="Session is running quietly.",
            process_status="running",
            session_id="shs_quiet",
        )
        register_runtime_compact_evidence(
            _view("batch-quiet", [row]).raw,
            single_compact=row["compact_tool_result"],
        )
        set_active_execution_control(
            metadata,
            turn_sequence=metadata["turn_sequence"],
            active_execution={
                "originating_tool_id": "shell.utility",
                "continuation_tool_id": "shell.write_stdin",
                "process_status": "running",
                "session_id": "shs_quiet",
                "stdin_available": True,
            },
        )
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()

    def wait_for_output(**_kwargs: Any) -> ShellSessionUpdate:
        nonlocal wait_calls
        wait_calls += 1
        return _shell_update(
            stdout="ordinary done\n",
            process_status=ShellProcessStatus.COMPLETED,
            session_status=ShellSessionLifecycleStatus.CLOSED,
            session_id=None,
            interaction_boundary=ShellInteractionBoundary.TERMINAL,
            exit_code=0,
        )

    state = _initial_shell_state(
        batch_id="batch-quiet",
        call_id="call-quiet-start",
        command="sleep 2",
    )
    dispatched = await dispatch(state.model_dump(mode="json"))
    graph = build_tool_execution_session_subgraph(
        decide_interaction_fn=lambda **_kwargs: {"action": "wait_for_output"},
        wait_interaction_fn=wait_for_output,
    )

    result = await graph.ainvoke(dispatched)
    runtime = read_compact_evidence(result["facts"]["metadata"], prefer_runtime=True)

    assert dispatch_calls == ["shell.utility"]
    assert wait_calls == 1
    assert runtime is not None
    assert [row["tool_id"] for row in runtime.rows] == [
        "shell.utility",
        "shell.utility",
    ]
    assert runtime.rows[-1]["compact_tool_result"]["stdout"] == "ordinary done\n"
    assert runtime.rows[-1]["compact_tool_result"]["process_status"] == "completed"
    assert "planner_plan" in result["facts"]["metadata"]
    assert read_execution_session_control(result["facts"]["metadata"]) is None


@pytest.mark.asyncio
async def test_wait_with_later_output_stays_in_subgraph_without_write_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted_events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_module, "get_stream_writer", lambda: emitted_events.append)
    wait_updates = [
        _shell_update(
            stdout="progress Authorization: Bearer durable-secret-token\n",
            stderr="progress warning\n",
            process_status=ShellProcessStatus.RUNNING,
            session_status=ShellSessionLifecycleStatus.ACTIVE,
            session_id="shs_later",
            interaction_boundary=ShellInteractionBoundary.OUTPUT_AVAILABLE,
            stdin_available=True,
        ),
        _shell_update(
            stdout="done\n",
            process_status=ShellProcessStatus.COMPLETED,
            session_status=ShellSessionLifecycleStatus.CLOSED,
            session_id=None,
            interaction_boundary=ShellInteractionBoundary.TERMINAL,
            exit_code=0,
        ),
    ]
    dispatch_calls: list[str] = []
    decisions: list[str] = []

    async def dispatch(state: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        dispatch_calls.append("shell.utility")
        row = _row(
            call_id="call-later-start",
            tool_id="shell.utility",
            summary="Session is running.",
            stdout="started\n",
            process_status="running",
            session_id="shs_later",
        )
        register_runtime_compact_evidence(
            _view("batch-later", [row]).raw,
            single_compact=row["compact_tool_result"],
        )
        set_active_execution_control(
            metadata,
            turn_sequence=metadata["turn_sequence"],
            active_execution={
                "originating_tool_id": "shell.utility",
                "continuation_tool_id": "shell.write_stdin",
                "process_status": "running",
                "session_id": "shs_later",
                "stdin_available": True,
            },
        )
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()

    def decide(**_kwargs: Any) -> dict[str, Any]:
        decisions.append("wait_for_output")
        return {"action": "wait_for_output"}

    def wait_for_output(**_kwargs: Any) -> ShellSessionUpdate:
        return wait_updates.pop(0)

    state = _initial_shell_state(
        batch_id="batch-later",
        call_id="call-later-start",
        command="python3 -u delayed_output.py",
    )
    dispatched = await dispatch(state.model_dump(mode="json"))
    graph = build_tool_execution_session_subgraph(
        decide_interaction_fn=decide,
        wait_interaction_fn=wait_for_output,
    )

    result = await graph.ainvoke(dispatched)
    runtime = read_compact_evidence(result["facts"]["metadata"], prefer_runtime=True)

    assert dispatch_calls == ["shell.utility"]
    assert decisions == []
    assert wait_updates == []
    assert runtime is not None
    assert [row["compact_tool_result"]["stdout"] for row in runtime.rows] == [
        "started\n",
        "progress Authorization: Bearer durable-secret-token\n",
        (
            "started\n"
            "progress Authorization: Bearer durable-secret-token\n"
            "done\n"
        ),
    ]
    assert runtime.rows[-1]["compact_tool_result"]["stderr"] == (
        "progress warning\n"
    )
    ptr_sections = extract_last_tool_sections(
        result["facts"]["metadata"],
        result["facts"],
        evidence_override=runtime,
    )
    assert ptr_sections["tool_output_summary"] == (
        "Command completed successfully.\n\n"
        "stdout:\nstarted\n"
        "progress Authorization: Bearer durable-secret-token\n"
        "done\n\n"
        "stderr:\nprogress warning"
    )
    assert all(row["tool_id"] == "shell.utility" for row in runtime.rows)
    assert [event["type"] for event in emitted_events] == ["tool_delta", "tool_end"]
    assert [event["tool_call_id"] for event in emitted_events] == [
        "call-later-start",
        "call-later-start",
    ]
    assert emitted_events[0]["process_status"] == "running"
    assert emitted_events[1]["process_status"] == "completed"
    assert "durable-secret-token" not in repr(emitted_events)
    assert "<DURABLE_SECRET_MASK:token>" in repr(emitted_events)
    assert read_execution_session_control(result["facts"]["metadata"]) is None


@pytest.mark.asyncio
async def test_cancelled_session_terminal_update_clears_graph_control() -> None:
    async def dispatch(state: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.ensure_metadata()
        row = _row(
            call_id="call-cancel-start",
            tool_id="shell.utility",
            summary="Session is running.",
            stdout="started\n",
            process_status="running",
            session_id="shs_cancel",
        )
        register_runtime_compact_evidence(
            _view("batch-cancel", [row]).raw,
            single_compact=row["compact_tool_result"],
        )
        set_active_execution_control(
            metadata,
            turn_sequence=metadata["turn_sequence"],
            active_execution={
                "originating_tool_id": "shell.utility",
                "continuation_tool_id": "shell.write_stdin",
                "process_status": "running",
                "session_id": "shs_cancel",
                "stdin_available": True,
            },
        )
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()

    state = _initial_shell_state(
        batch_id="batch-cancel",
        call_id="call-cancel-start",
        command="python3 -u cancellable.py",
    )
    dispatched = await dispatch(state.model_dump(mode="json"))
    graph = build_tool_execution_session_subgraph(
        decide_interaction_fn=lambda **_kwargs: {"action": "wait_for_output"},
        wait_interaction_fn=lambda **_kwargs: _shell_update(
            process_status=ShellProcessStatus.TERMINATED,
            session_status=ShellSessionLifecycleStatus.CLOSED,
            session_id=None,
            interaction_boundary=ShellInteractionBoundary.TERMINAL,
            success=False,
            status="error",
            error_code=ShellSessionErrorCode.SESSION_UNAVAILABLE,
        ),
    )

    result = await graph.ainvoke(dispatched)
    runtime = read_compact_evidence(result["facts"]["metadata"], prefer_runtime=True)

    assert runtime is not None
    terminal = runtime.rows[-1]["compact_tool_result"]
    assert terminal["success"] is False
    assert terminal["process_status"] == "terminated"
    assert terminal["session_status"] == "closed"
    assert terminal["interaction_boundary"] == "terminal"
    assert terminal["session_id"] is None
    assert read_shell_interaction_transcript("batch-cancel") is None
    assert read_execution_session_control(result["facts"]["metadata"]) is None


@pytest.mark.asyncio
async def test_terminal_interactive_aggregate_is_compressed_once_before_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the terminal aggregate enters the standard compression boundary."""

    batch_id = "batch-terminal-compression"
    aggregate = {
        "tool_batch_id": batch_id,
        "execution_session_aggregate": True,
        "status": "completed",
        "success": True,
        "results": [
            _row(
                call_id="call-start",
                tool_id="shell.assessment",
                summary="Session is running.",
                stdout="READY\n",
                process_status="running",
                session_id="shs-terminal-compression",
            ),
            _row(
                call_id="call-finish",
                tool_id="shell.write_stdin",
                summary="Command completed.",
                stdout="READY\n7\n42\n50\n",
                stderr="warning\n",
                process_status="completed",
                session_id=None,
                exit_code=0,
            ),
        ],
        "deferred_followups": [],
    }
    artifact_path = "artifacts/shell-assessment-shs-terminal-compression.txt"
    aggregate["results"][-1]["compact_tool_result"].update(
        {
            "artifacts": [artifact_path],
            "artifact_refs": [{"path": artifact_path, "count": 1}],
            "metadata": {
                "artifact_scope": "runtime_workspace",
                "runtime_session": {
                    "originating_capability": "assessment",
                    "artifact_capture": {
                        "status": "succeeded",
                        "artifact_count": 1,
                    },
                },
            },
        }
    )
    register_runtime_compact_evidence(
        aggregate,
        single_compact=aggregate["results"][-1]["compact_tool_result"],
    )
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run a calculator session.",
            metadata={"turn_sequence": 7, "tool_batch_id": batch_id},
        ),
        trace=TraceState(),
    )
    compression_calls: list[dict[str, Any]] = []

    class _Compact:
        compression = SimpleNamespace(source="llm", fallback_reason=None)

        def to_dict(self) -> dict[str, Any]:
            return {
                "schema_version": "2.0",
                "tool": "shell.assessment",
                "status": "success",
                "success": True,
                "exit_code": 0,
                "summary": "Calculator returned all requested values.",
                "key_findings": ["7", "42", "50"],
                "errors": [],
                "report_recommendations": [],
                "structured_signals": [],
                "decision_evidence": ["7\n42\n50"],
                "lossiness_risk": "low",
                "artifact_refs": [],
                "compression": {"source": "llm", "fallback_reason": None},
            }

    async def _compress(**kwargs: Any) -> SimpleNamespace:
        compression_calls.append(dict(kwargs))
        return SimpleNamespace(compact_output=_Compact(), usage_record=None)

    monkeypatch.setattr(terminal_compressor_module, "compress_tool_output", _compress)
    monkeypatch.setattr(
        terminal_compressor_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        terminal_compressor_module,
        "compact_output_size_bytes",
        lambda _compact: 256,
    )
    monkeypatch.setattr(
        terminal_compressor_module,
        "record_compression_observability_metrics",
        lambda **_kwargs: None,
    )

    updated = await compress_terminal_execution_session_output(state)
    runtime = read_compact_evidence(
        updated["facts"]["metadata"],
        prefer_runtime=True,
    )

    assert len(compression_calls) == 1
    assert compression_calls[0]["tool_name"] == "shell.assessment"
    assert compression_calls[0]["raw_result"]["stdout"] == "READY\n7\n42\n50\n"
    assert compression_calls[0]["raw_result"]["stderr"] == "warning\n"
    assert compression_calls[0]["raw_result"]["artifacts"] == [artifact_path]
    assert runtime is not None
    terminal = runtime.rows[-1]["compact_tool_result"]
    assert terminal["summary"] == "Calculator returned all requested values."
    assert terminal["key_findings"] == ["7", "42", "50"]
    assert terminal["process_status"] == "completed"
    assert terminal["artifacts"] == [artifact_path]
    assert terminal["artifact_refs"] == [{"path": artifact_path, "count": 1}]
    assert terminal["metadata"]["artifact_scope"] == "runtime_workspace"
    assert "stdout" not in terminal
    assert "stderr" not in terminal
    ptr_sections = extract_last_tool_sections(
        updated["facts"]["metadata"],
        updated["facts"],
        evidence_override=runtime,
    )
    assert ptr_sections["tool_output_summary"] == (
        "Calculator returned all requested values."
    )
    assert "7" in ptr_sections["key_findings"]
    assert "42" in ptr_sections["key_findings"]
    assert "50" in ptr_sections["key_findings"]

    await compress_terminal_execution_session_output(updated)
    assert len(compression_calls) == 1


@pytest.mark.asyncio
async def test_terminal_compressor_leaves_ordinary_tool_aggregate_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary terminal tools retain their existing single compression pass."""

    batch_id = "batch-ordinary-terminal"
    row = _row(
        call_id="call-ordinary",
        tool_id="shell.utility",
        summary="Command completed immediately.",
        stdout="done\n",
        process_status="completed",
        session_id=None,
        exit_code=0,
    )
    aggregate = {
        "tool_batch_id": batch_id,
        "execution_session_aggregate": True,
        "status": "completed",
        "success": True,
        "results": [row],
        "deferred_followups": [],
    }
    register_runtime_compact_evidence(
        aggregate,
        single_compact=row["compact_tool_result"],
    )
    state = InteractiveState(
        facts=FactsState(
            task_id=42,
            message="Run a normal command.",
            metadata={"turn_sequence": 7, "tool_batch_id": batch_id},
        ),
        trace=TraceState(),
    )
    compression_calls: list[dict[str, Any]] = []

    async def _compress(**kwargs: Any) -> None:
        compression_calls.append(dict(kwargs))

    monkeypatch.setattr(terminal_compressor_module, "compress_tool_output", _compress)

    updated = await compress_terminal_execution_session_output(state)
    runtime = read_compact_evidence(
        updated["facts"]["metadata"],
        prefer_runtime=True,
    )

    assert compression_calls == []
    assert runtime is not None
    assert runtime.rows[-1]["compact_tool_result"] == row["compact_tool_result"]
