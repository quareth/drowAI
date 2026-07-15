"""Tenant baseline local parity matrix for runtime decoupling.

This suite executes the local parity matrix by running targeted regression
tests for each tenant_baseline behavior slice and classifying failures by boundary
domain (provider, tenant baseline, state transition, local Docker behavior).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[3]

@dataclass(frozen=True)
class _ParitySlice:
    surface: str
    failure_domain: str
    test_node_ids: tuple[str, ...]


_PARITY_MATRIX: tuple[_ParitySlice, ...] = (
    _ParitySlice(
        surface="task create/start plus manual start gating on provider-confirmed availability",
        failure_domain="state_transition",
        test_node_ids=(
            "backend/tests/test_task_services_refactor.py::test_lifecycle_service_creates_task_and_delegates_bootstrap_and_queue",
            "backend/tests/test_task_services_refactor.py::test_lifecycle_bootstrap_materializes_workspace_through_runtime_provider",
            "backend/tests/test_task_services_refactor.py::test_lifecycle_materialization_failure_marks_task_failed_and_skips_queue",
            "backend/tests/test_task_services_refactor.py::test_lifecycle_initialization_uses_provider_success_path",
        ),
    ),
    _ParitySlice(
        surface="provider-owned workspace/config/scope/VPN materialization in local mode",
        failure_domain="provider",
        test_node_ids=(
            "backend/tests/services/test_runtime_provider_contracts.py::test_runtime_request_and_result_include_identity_fields",
            "backend/tests/services/runtime_provider/test_local_docker_provider.py::test_materialize_vpn_config_writes_local_runtime_file",
            "backend/tests/routers/test_task_scope_route.py::test_task_scope_route_parses_provider_resolved_scope_file",
            "backend/tests/routers/test_task_scope_route.py::test_task_scope_route_falls_back_to_durable_scope_when_file_missing",
            "backend/tests/test_task_services_refactor.py::test_lifecycle_restart_materialization_reuses_persisted_vpn_config",
        ),
    ),
    _ParitySlice(
        surface="pause/resume/stop/retire lifecycle and hard-delete runtime teardown parity",
        failure_domain="state_transition",
        test_node_ids=(
            "backend/tests/test_container_lifecycle.py::test_container_lifecycle_controls_simulated_mode",
            "backend/tests/test_tasks_router_phase3_cleanup.py::test_container_create_endpoint_preserves_generic_error_contract",
            "backend/tests/test_tasks_router_phase3_cleanup.py::test_container_status_endpoint_uses_string_status_contract",
            "backend/tests/test_tasks_router_phase3_cleanup.py::test_task_delete_endpoint_uses_cleanup_service_contract",
        ),
    ),
    _ParitySlice(
        surface="/api/docker task-scoped logs/progress/exec/stop/metrics/status plus terminal authorization",
        failure_domain="local_docker",
        test_node_ids=(
            "backend/tests/test_docker_router_route_continuity.py::test_docker_rest_routes_are_still_mounted",
            "backend/tests/routers/test_docker_logs_rest.py::test_docker_routes_preserve_authorization_http_exception",
            "backend/tests/routers/test_docker_terminal_sessions.py::test_create_terminal_session_reraises_http_exception",
            "backend/tests/routers/test_docker_terminal_sessions.py::test_close_terminal_session_keeps_not_found_status",
        ),
    ),
    _ParitySlice(
        surface="container listing/task-scoped compatibility authorization and default tenant/task ownership baseline",
        failure_domain="tenant_baseline",
        test_node_ids=(
            "backend/tests/services/test_ws_gateway_authorize.py::test_enforce_ws_task_ownership_denies_non_owner_with_forbidden_task_payload",
            "backend/tests/architecture/test_tenant_baseline_ownership_paths.py::test_tenant_baseline_task_scoped_query_surface_blocks_cross_tenant_task_id_only_reads",
            "backend/tests/architecture/test_tenant_baseline_ownership_paths.py::test_tenant_baseline_engagement_scoped_query_surface_filters_to_owner_boundary",
        ),
    ),
    _ParitySlice(
        surface="terminal session path plus terminal websocket input/resize parity",
        failure_domain="provider",
        test_node_ids=(
            "backend/tests/test_terminal_session_manager_refactor.py::test_terminal_session_manager_facade_reexports_public_surface",
            "backend/tests/test_terminal_session_manager_refactor.py::test_terminal_session_active_io_methods_are_disabled",
            "backend/tests/test_terminal_ws_handler_identity.py::test_handle_terminal_ws_rejects_missing_identity",
            "backend/tests/services/runtime_provider/test_local_docker_provider.py::test_terminal_read_output_is_provider_mediated",
        ),
    ),
    _ParitySlice(
        surface="canonical /ws docker|terminal|metrics and /api/tasks/ws/tasks/{task_id}/metrics alias delegation",
        failure_domain="tenant_baseline",
        test_node_ids=(
            "backend/tests/test_main_websocket_task_ownership.py::test_single_task_channels_deny_non_owner",
            "backend/tests/test_main_websocket_task_ownership.py::test_websocket_endpoint_routes_metrics_channel",
            "backend/tests/test_metrics_ws_alias_deprecation.py::test_metrics_alias_emits_deprecation_headers_and_log_on_authorized_path",
        ),
    ),
    _ParitySlice(
        surface="shell/filesystem PTY read-write and retained named-session close path",
        failure_domain="provider",
        test_node_ids=(
            "agent/tests/test_http_tool_pty.py::test_http_request_routes_via_pty_and_parses_metadata",
            "agent/tests/test_http_tool_pty.py::test_http_download_routes_via_pty_and_returns_download_metadata",
            "agent/tests/test_tool_pty_support.py::test_http_request_supports_pty_and_builds_command",
            "agent/tests/test_tool_pty_support.py::test_http_download_supports_pty_and_uses_relative_output",
        ),
    ),
    _ParitySlice(
        surface="langgraph runtime warmup PTY preparation during HITL wait windows",
        failure_domain="provider",
        test_node_ids=(
            "backend/tests/routers/test_chat_prewarm_ready.py::test_chat_prewarm_triggers_runtime_warmup",
            "backend/tests/langgraph_chat/test_runtime_warmup_service.py::test_warm_task_runtime_is_idempotent_for_successful_steps",
        ),
    ),
    _ParitySlice(
        surface="logs/metrics streaming plus reasoning SSE/history fallback compatibility",
        failure_domain="local_docker",
        test_node_ids=(
            "backend/tests/services/test_ws_log_streamer.py::test_stream_logs_sanitizes_generic_errors",
            "backend/tests/services/test_ws_metrics_streamer.py::test_serve_metrics_websocket_runs_single_lifecycle_path",
            "backend/tests/services/streaming/test_reasoning_sse_service.py::test_generate_file_based_events_emits_idle_comment_when_tail_stays_quiet",
            "backend/tests/services/test_agent_reasoning_history_service.py::test_file_history_normalizes_entries_and_preserves_existing_desc_after_behavior",
        ),
    ),
    _ParitySlice(
        surface="runtime input append-and-signal contract",
        failure_domain="provider",
        test_node_ids=(
            "backend/tests/services/task/test_runtime_input_service.py::test_strict_persistence_stops_before_signal_on_append_error",
            "backend/tests/services/task/test_runtime_input_service.py::test_best_effort_persistence_still_signals_when_append_fails",
            "backend/tests/services/task/test_runtime_input_service.py::test_successful_append_writes_runtime_input_and_surfaces_signal_failure",
        ),
    ),
    _ParitySlice(
        surface="chat tool metadata with GraphRuntimeContext and thread config runtime projection fields",
        failure_domain="provider",
        test_node_ids=(
            "backend/tests/langgraph_chat/test_context_builder.py::test_build_runtime_config_projects_runtime_identity_into_graph_context",
            "backend/tests/langgraph_chat/test_turn_runtime_helpers.py::test_apply_agent_thread_config_writes_canonical_graph_and_turn_fields",
            "backend/tests/langgraph_chat/test_facade_checkpointing.py::test_build_checkpoint_execution_config_carries_provider_runtime_for_invocation",
        ),
    ),
    _ParitySlice(
        surface="graph/tool command dispatch and workspace fallback fail-closed runtime boundary",
        failure_domain="provider",
        test_node_ids=(
            "agent/tests/test_tool_execution_runtime_provider_context.py::test_graph_tool_dispatch_enters_runtime_provider",
            "agent/tests/test_tool_execution_runtime_provider_context.py::test_graph_tool_dispatch_fails_closed_without_runtime_identity",
        ),
    ),
    _ParitySlice(
        surface="HITL resume/retry carrier construction and runtime projection metadata",
        failure_domain="state_transition",
        test_node_ids=(
            "backend/tests/langgraph_chat/test_facade_checkpointing.py::test_continuation_rebuilds_runtime_dependencies_from_user_id",
            "backend/tests/langgraph_chat/test_facade_checkpointing.py::test_continuation_resolves_checkpoint_runtime_hint",
            "backend/tests/e2e/test_hitl_resume_simple_tool.py::test_simple_tool_router_refresh_then_resume_with_edit",
        ),
    ),
    _ParitySlice(
        surface="conversation metadata boundary, intent persistence boundary, and runtime environment metadata context boundary",
        failure_domain="provider",
        test_node_ids=(
            "backend/tests/test_llm_api.py::test_create_conversation_keeps_openai_lifecycle_behavior",
            "backend/tests/test_llm_api.py::test_reset_conversation_fails_before_openai_side_effects_without_capability",
            "backend/tests/langgraph_chat/test_intent_persistence_management_state.py::test_persist_intent_context_writes_management_state",
            "backend/tests/services/runtime_provider/test_local_docker_provider.py::test_runtime_environment_metadata_roundtrip_uses_provider_boundary",
        ),
    ),
    _ParitySlice(
        surface="runtime-produced artifact capture/public read/adapter read/archive file boundary",
        failure_domain="local_docker",
        test_node_ids=(
            "backend/tests/services/test_artifact_provenance_service.py::test_complete_tool_execution_reads_artifact_paths_through_provider",
            "backend/tests/routers/test_artifact_provenance_router.py::test_read_task_artifact_endpoint_supports_inline_and_file_modes",
            "backend/tests/services/test_knowledge_archive_service.py::test_archive_service_writes_archived_file_to_engagement_owned_durable_path",
            "backend/tests/integration/test_langgraph_artifact_provenance.py::test_artifact_retrieval_tools_do_not_emit_followup_tool_artifacts",
        ),
    ),
    _ParitySlice(
        surface="file browser provider/workspace query dispatch and task ownership boundary",
        failure_domain="tenant_baseline",
        test_node_ids=(
            "backend/tests/services/test_ws_gateway_authorize.py::test_enforce_ws_task_ownership_denies_non_owner_with_forbidden_task_payload",
            "backend/tests/test_file_browser_api.py::test_files_endpoints_validate_task_ownership",
        ),
    ),
)


def _run_matrix_slice(slice_: _ParitySlice) -> subprocess.CompletedProcess[str]:
    # Isolate nested pytest database state from the parent session.
    # Each subprocess gets its own sqlite file so teardown cannot clobber
    # the shared default backend_test.sqlite3 used by the outer test run.
    with tempfile.TemporaryDirectory(prefix="tenant-baseline-parity-db-") as temp_dir:
        db_path = Path(temp_dir) / "backend_test.sqlite3"
        db_url = f"sqlite:///{db_path.as_posix()}"
        env = os.environ.copy()
        env["BACKEND_TEST_DATABASE_URL"] = db_url
        env["DATABASE_URL"] = db_url

        command = [sys.executable, "-m", "pytest", "-q", "--maxfail=1", *slice_.test_node_ids]
        return subprocess.run(
            command,
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.mark.parametrize(
    "slice_",
    _PARITY_MATRIX,
    ids=[f"{item.failure_domain}:{item.surface}" for item in _PARITY_MATRIX],
)
def test_tenant_baseline_local_parity_matrix(slice_: _ParitySlice) -> None:
    """tenant_baseline parity artifact: execute representative behavior checks per parity slice."""
    result = _run_matrix_slice(slice_)

    assert result.returncode == 0, (
        f"tenant_baseline parity slice failed [{slice_.failure_domain}] {slice_.surface}\n"
        f"Executed: {' '.join(slice_.test_node_ids)}\n"
        f"pytest exit code: {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
