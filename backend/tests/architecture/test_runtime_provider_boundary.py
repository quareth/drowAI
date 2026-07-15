"""Runtime provider architecture boundary tests.

Responsibilities:
- Enforce Phase 1 runtime-provider import boundaries for migrated modules.
- Keep a phase-indexed denylist harness that later phases can extend in place.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import pathlib
import re
from collections.abc import Iterable, Sequence


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# Add new migrated module paths to later phase tuples as each surface moves
# behind TaskExecutionRuntimeProvider.
_MIGRATED_MODULES_BY_PHASE: dict[str, tuple[str, ...]] = {
    "1": (
        "backend/services/runtime_provider/__init__.py",
        "backend/services/runtime_provider/contracts.py",
        "backend/services/runtime_provider/provider.py",
        "backend/services/runtime_provider/registry.py",
        "backend/config/feature_flags.py",
    ),
    "3": (
        "backend/routers/tasks/container.py",
        "backend/routers/tasks/crud.py",
        "backend/services/task/cleanup_service.py",
        "backend/services/task/lifecycle_service.py",
        "backend/services/task/retirement_service.py",
        "backend/services/task/runtime_input_service.py",
        "backend/services/task/runtime_service.py",
    ),
    "4": (
        "backend/routers/chat/__init__.py",
        "backend/routers/chat/readiness.py",
        "backend/routers/chat/submit.py",
        "backend/services/langgraph_chat/context_builder.py",
        "backend/services/langgraph_chat/intent/persistence.py",
        "backend/services/streaming/log_watcher.py",
        "agent/graph/nodes/decision_router/pause.py",
        "agent/graph/subgraphs/tool_execution_runtime/request_context.py",
    ),
    "5": (
        "backend/routers/agent_reasoning.py",
        "backend/routers/docker_logs_rest.py",
        "backend/routers/docker_terminal_sessions.py",
        "backend/routers/docker_ws_alias.py",
        "backend/routers/llm.py",
        "backend/routers/tasks/files.py",
        "backend/routers/tasks/metrics.py",
        "backend/routers/tasks/runtime.py",
        "backend/routers/tasks/scope.py",
        "backend/routers/tasks/vpn.py",
        "backend/services/langgraph_chat/execution/graph_executor.py",
        "backend/services/langgraph_chat/runtime/warmup_service.py",
        "backend/services/streaming/reasoning_history_service.py",
        "backend/services/streaming/reasoning_sse_service.py",
        "backend/services/workspace/file_browser_service.py",
        "backend/services/knowledge/adapter_registry.py",
        "backend/services/knowledge/ingestion_service.py",
        "backend/services/terminal/manager.py",
        "backend/services/terminal/ws_handler.py",
        "backend/services/websocket/log_streamer.py",
        "backend/services/websocket/metrics_streamer.py",
        "agent/tools/exploitation_tools/metasploit/interactive_executor.py",
        "agent/tools/exploitation_tools/metasploit/msf_session_manager.py",
        "agent/tools/shell/_pty_executor.py",
    ),
    "7": (
        "agent/graph/nodes/decision_router/pause.py",
        "agent/graph/subgraphs/tool_execution_runtime/request_context.py",
        "agent/tools/exploitation_tools/metasploit/interactive_executor.py",
        "agent/tools/exploitation_tools/metasploit/msf_session_manager.py",
        "agent/tools/shell/_pty_executor.py",
        "backend/routers/agent_reasoning.py",
        "backend/routers/chat/__init__.py",
        "backend/routers/chat/readiness.py",
        "backend/routers/chat/submit.py",
        "backend/routers/docker_logs_rest.py",
        "backend/routers/docker_terminal_sessions.py",
        "backend/routers/docker_ws_alias.py",
        "backend/routers/llm.py",
        "backend/routers/tasks/container.py",
        "backend/routers/tasks/crud.py",
        "backend/routers/tasks/files.py",
        "backend/routers/tasks/metrics.py",
        "backend/routers/tasks/runtime.py",
        "backend/routers/tasks/scope.py",
        "backend/routers/tasks/vpn.py",
        "backend/services/langgraph_chat/context_builder.py",
        "backend/services/langgraph_chat/execution/graph_executor.py",
        "backend/services/langgraph_chat/intent/persistence.py",
        "backend/services/langgraph_chat/runtime/warmup_service.py",
        "backend/services/knowledge/adapter_registry.py",
        "backend/services/knowledge/ingestion_service.py",
        "backend/services/streaming/log_watcher.py",
        "backend/services/streaming/reasoning_history_service.py",
        "backend/services/streaming/reasoning_sse_service.py",
        "backend/services/task/cleanup_service.py",
        "backend/services/task/lifecycle_service.py",
        "backend/services/task/retirement_service.py",
        "backend/services/task/runtime_input_service.py",
        "backend/services/task/runtime_service.py",
        "backend/services/terminal/manager.py",
        "backend/services/terminal/ws_handler.py",
        "backend/services/workspace/file_browser_service.py",
        "backend/services/websocket/log_streamer.py",
        "backend/services/websocket/metrics_streamer.py",
    ),
}

# Final Tenant baseline boundary enforcement spans all migrated module phases.
_ENFORCED_PHASES: tuple[str, ...] = ("1", "3", "4", "5", "7")

_FORBIDDEN_RUNTIME_AUTHORITY_IMPORTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(from|import)\s+backend\.services\.unified_docker_service\b"),
    re.compile(r"^\s*(from|import)\s+backend\.services\.docker\b"),
    re.compile(r"^\s*from\s+backend\.services\s+import\s+unified_docker_service\b"),
    re.compile(
        r'backend\.services\.__getattr__\(\s*["\']unified_docker_service["\']\s*\)'
    ),
    re.compile(r"^\s*(from|import)\s+backend\.services\.container_utils\b"),
    re.compile(
        r"\bcontainer_utils\.(?:get_container_name|get_workspace_path|get_container_info|get_container_status|get_container_stats|get_container_logs)\b"
    ),
    re.compile(r"^\s*(from|import)\s+docker\b"),
)

_FORBIDDEN_WORKSPACE_MATERIALIZATION_REFERENCES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bWorkspaceManager\b"),
    re.compile(r"\bWorkspaceConfig\b"),
    re.compile(r"\bget_task_workspace_path\("),
    re.compile(r"\bget_workspace_path\("),
    re.compile(r"\bget_container_workspace_path\("),
)

_ACTIVE_TERMINAL_IO_MODULES: tuple[str, ...] = (
    "backend/services/terminal/manager.py",
    "backend/services/terminal/ws_handler.py",
    "agent/tools/shell/_pty_executor.py",
    "agent/tools/exploitation_tools/metasploit/interactive_executor.py",
    "agent/tools/exploitation_tools/metasploit/msf_session_manager.py",
)

_FORBIDDEN_TERMINAL_SESSION_IO: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:session|terminal_session)\.(?:read|write)\("),
)

_RUNTIME_ARTIFACT_READ_MODULES: tuple[str, ...] = (
    "backend/services/artifact/memory_service.py",
    "backend/routers/artifact_provenance.py",
    "backend/services/artifact/provenance_service.py",
    "backend/services/knowledge/archive_service.py",
    "backend/services/knowledge/adapter_registry.py",
    "backend/services/knowledge/ingestion_service.py",
)

_FORBIDDEN_TASK_WORKSPACE_RECONSTRUCTION: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bget_task_workspace_path\("),
)

_ROUTE_WORKSPACE_RECONSTRUCTION_MODULES: tuple[str, ...] = (
    "backend/routers/tasks/files.py",
    "backend/routers/tasks/scope.py",
)

_FORBIDDEN_ROUTE_WORKSPACE_RECONSTRUCTION: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmaterialize_runtime_workspace\("),
    re.compile(r'delegate\.get\("workspace_path"\)'),
    re.compile(r"Path\(str\(workspace_path\)\)"),
)

_MANAGEMENT_PLANE_DOC_PATH = (
    _REPO_ROOT / "docs/architecture/management-plane.md"
)
_RUNNER_CHANNEL_DOC_PATH = (
    _REPO_ROOT / "docs/architecture/runtime-provider.md"
)
_RUNNER_CONTROL_ROUTER_PATH = _REPO_ROOT / "backend/routers/runner_control.py"
_RUNNER_APP_PATH = _REPO_ROOT / "drowai_runner/app.py"
_RUNNER_CONFIG_PATH = _REPO_ROOT / "drowai_runner/config.py"
_RUNTIME_SNAPSHOT_NORMALIZER_PATH = (
    _REPO_ROOT / "backend/services/runtime_provider/snapshot_normalization.py"
)

_LOCAL_PROVIDER_DEV_TEST_ALLOWLIST: dict[str, str] = {
    "backend/tests/services/runtime_provider/test_local_docker_provider.py": (
        "local provider implementation coverage"
    ),
    "backend/tests/test_container_lifecycle.py": (
        "local UnifiedDockerService simulation coverage"
    ),
    "backend/tests/test_env_collection_integration.py": (
        "local UnifiedDockerService environment collection coverage"
    ),
    "backend/tests/test_env_info_integration.py": (
        "local provider environment info flow coverage"
    ),
    "tests/test_frontend_backend_unified.py": (
        "developer local UnifiedDockerService diagnostic script"
    ),
    "tests/test_unified_docker.py": "developer local Docker smoke test",
    "tests/test_task_workspace_isolation.py": (
        "developer local Docker workspace isolation smoke test"
    ),
    "tests/test_complete_workspace_isolation.py": (
        "developer local Docker workspace isolation smoke test"
    ),
}

_LOCAL_PROVIDER_AUTHORITY_MODULES: tuple[str, ...] = (
    "backend.services.runtime_provider.local_docker_provider",
    "backend.services.unified_docker_service",
)
_LOCAL_PROVIDER_AUTHORITY_NAMES: tuple[str, ...] = (
    "LocalDockerRuntimeProvider",
    "unified_docker_service",
)

_RUNTIME_SIDE_EFFECT_METHODS: frozenset[str] = frozenset(
    {
        "provision_task_runtime",
        "materialize_runtime_workspace",
        "materialize_vpn_config",
        "retry_vpn_connection",
        "pause_task_runtime",
        "resume_task_runtime",
        "stop_task_runtime",
        "retire_task_runtime",
        "execute_tool_command",
        "start_terminal_session",
        "read_runtime_artifact_file",
        "write_runtime_artifact_file",
        "query_runtime_artifacts",
    }
)
_RUNTIME_OPERATION_DISPATCH_METHODS: frozenset[str] = frozenset(
    {
        "run_for_context",
        "run_authorized_task_operation",
        "run_user_task_operation",
        "_run_task_runtime_operation",
    }
)
_PROVIDER_SELECTION_METHODS: frozenset[str] = frozenset(
    {"get_provider", "get_provider_for_task"}
)
_RUNTIME_SIDE_EFFECT_SCAN_ROOTS: tuple[str, ...] = (
    "backend/routers",
    "backend/services",
)
_RUNTIME_SIDE_EFFECT_BYPASS_ALLOWLIST: dict[str, str] = {
    "backend/routers/docker_logs_rest.py": (
        "diagnostic-only local Docker compose status endpoint"
    ),
}
_PRODUCT_POLICY_MODULE_PATH = "backend/services/runtime_provider/product_policy.py"

@dataclass(frozen=True)
class _AllowlistedException:
    match_pattern: re.Pattern[str]
    todo: str
    owner: str
    future_owner: str
    removal_condition: str


_ALLOWLISTED_EXCEPTIONS: tuple[_AllowlistedException, ...] = (
)

_FUTURE_OWNER_RE = re.compile(
    r"^(runner_control|tooling_plane|execution_plane|remote_runtime|data_plane|tenant_baseline|tenant_isolation|cutover)\+?$"
)


def _resolve_module_paths(phases: Sequence[str]) -> list[pathlib.Path]:
    rel_paths: list[str] = []
    for phase in phases:
        rel_paths.extend(_MIGRATED_MODULES_BY_PHASE.get(phase, ()))
    unique_rel_paths = tuple(dict.fromkeys(rel_paths))
    return [_REPO_ROOT / rel_path for rel_path in unique_rel_paths]


def _filter_allowlisted(matches: Sequence[str]) -> tuple[list[str], list[str]]:
    remaining: list[str] = []
    allowlisted: list[str] = []
    for match in matches:
        if any(entry.match_pattern.search(match) for entry in _ALLOWLISTED_EXCEPTIONS):
            allowlisted.append(match)
            continue
        remaining.append(match)
    return remaining, allowlisted


def _match_lines(path: pathlib.Path, patterns: Iterable[re.Pattern[str]]) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    matches: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        for pattern in patterns:
            if pattern.search(line):
                rel_path = path.relative_to(_REPO_ROOT)
                matches.append(f"{rel_path}:{line_number}: {line.strip()}")
                break
    return matches


def _collect_direct_local_provider_test_importers() -> set[str]:
    importers: set[str] = set()
    for root in (_REPO_ROOT / "backend/tests", _REPO_ROOT / "tests"):
        for py_file in sorted(root.rglob("*.py")):
            tree = ast.parse(
                py_file.read_text(encoding="utf-8-sig"),
                filename=str(py_file),
            )
            rel_path = str(py_file.relative_to(_REPO_ROOT))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(
                        alias.name == module for module in _LOCAL_PROVIDER_AUTHORITY_MODULES
                        for alias in node.names
                    ):
                        importers.add(rel_path)
                        break
                elif isinstance(node, ast.ImportFrom):
                    if node.level != 0 or not node.module:
                        continue
                    imported_names = {alias.name for alias in node.names}
                    if (
                        node.module in _LOCAL_PROVIDER_AUTHORITY_MODULES
                        or (
                            node.module == "backend.services.runtime_provider"
                            and imported_names
                            & set(_LOCAL_PROVIDER_AUTHORITY_NAMES)
                        )
                        or (
                            node.module == "backend.services"
                            and "unified_docker_service" in imported_names
                        )
                    ):
                        importers.add(rel_path)
                        break
    return importers


def _module_docstring(path: pathlib.Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return ast.get_docstring(tree) or ""


def _iter_backend_scan_files(roots: Sequence[str]) -> Iterable[pathlib.Path]:
    for rel_root in roots:
        root = _REPO_ROOT / rel_root
        for py_file in sorted(root.rglob("*.py")):
            if "backend/tests" in py_file.parts:
                continue
            yield py_file


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _is_runtime_operation_lambda(
    node: ast.Call,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    current: ast.AST | None = node
    lambda_node: ast.Lambda | None = None
    while current is not None:
        if isinstance(current, ast.Lambda):
            lambda_node = current
            break
        current = parents.get(current)
    if lambda_node is None:
        return False

    parent = parents.get(lambda_node)
    if not isinstance(parent, ast.keyword) or parent.arg != "call":
        return False
    call_node = parents.get(parent)
    if not isinstance(call_node, ast.Call):
        return False
    func = call_node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _RUNTIME_OPERATION_DISPATCH_METHODS
    )


def _collect_runtime_side_effect_bypasses() -> list[str]:
    offenders: list[str] = []
    for py_file in _iter_backend_scan_files(_RUNTIME_SIDE_EFFECT_SCAN_ROOTS):
        rel_path = str(py_file.relative_to(_REPO_ROOT))
        if rel_path.startswith("backend/services/runtime_provider/"):
            continue
        if rel_path in _RUNTIME_SIDE_EFFECT_BYPASS_ALLOWLIST:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8-sig"), filename=str(py_file))
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method in _PROVIDER_SELECTION_METHODS:
                offenders.append(
                    f"{rel_path}:{node.lineno}: direct provider selection `{method}`"
                )
            elif method in _RUNTIME_SIDE_EFFECT_METHODS and not _is_runtime_operation_lambda(
                node,
                parents,
            ):
                offenders.append(
                    f"{rel_path}:{node.lineno}: direct provider operation `{method}`"
                )
    return offenders


def _collect_product_local_policy_duplicates() -> list[str]:
    offenders: list[str] = []
    for py_file in _iter_backend_scan_files(_RUNTIME_SIDE_EFFECT_SCAN_ROOTS):
        rel_path = str(py_file.relative_to(_REPO_ROOT))
        if rel_path == _PRODUCT_POLICY_MODULE_PATH:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8-sig"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_source = ast.unparse(node.test)
            has_product_scope = (
                "RuntimeCallScope.PRODUCT" in test_source
                or "product_task" in test_source
            )
            has_local_placement = (
                "RuntimePlacementMode.LOCAL" in test_source
                or "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN" in test_source
            )
            if has_product_scope and has_local_placement:
                offenders.append(
                    f"{rel_path}:{node.lineno}: duplicated product/local placement policy"
                )
    return offenders


def test_phase1_boundary_blocks_runtime_authority_imports_outside_local_provider() -> None:
    """Phase 1 lock: migrated modules do not import Docker/runtime authority directly."""
    offenders: list[str] = []
    for module_path in _resolve_module_paths(_ENFORCED_PHASES):
        offenders.extend(
            _match_lines(module_path, _FORBIDDEN_RUNTIME_AUTHORITY_IMPORTS)
        )
    offenders, _allowlisted = _filter_allowlisted(offenders)

    assert not offenders, (
        "Phase 1 migrated modules must not import `unified_docker_service`, "
        "`backend.services.docker.*`, or Docker SDK directly. Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_phase1_boundary_blocks_workspace_materialization_outside_local_provider() -> None:
    """Phase 1 lock: migrated modules do not reconstruct provider-owned workspace paths."""
    offenders: list[str] = []
    for module_path in _resolve_module_paths(_ENFORCED_PHASES):
        offenders.extend(
            _match_lines(
                module_path,
                _FORBIDDEN_WORKSPACE_MATERIALIZATION_REFERENCES,
            )
        )
    offenders, allowlisted = _filter_allowlisted(offenders)

    assert not offenders, (
        "Phase 1 migrated modules must not materialize local runtime workspace "
        "details directly. Found:\n  - "
        + "\n  - ".join(offenders)
        + (
            "\nAllowlisted temporary exceptions:\n  - " + "\n  - ".join(allowlisted)
            if allowlisted
            else ""
        )
    )


def test_phase1_allowlist_keeps_unified_docker_service_import_local_to_provider_impl() -> None:
    """Phase 1 lock: local provider implementation remains the only provider package importer."""
    provider_package = _REPO_ROOT / "backend/services/runtime_provider"
    offenders: list[str] = []
    expected_owner = "backend/services/runtime_provider/local_docker_provider.py"

    for module_path in sorted(provider_package.rglob("*.py")):
        matches = _match_lines(module_path, _FORBIDDEN_RUNTIME_AUTHORITY_IMPORTS)
        if not matches:
            continue
        rel_path = str(module_path.relative_to(_REPO_ROOT))
        if rel_path != expected_owner:
            offenders.extend(matches)

    assert not offenders, (
        "Only `local_docker_provider.py` may import direct Docker/runtime authority "
        "inside `backend/services/runtime_provider`. Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_product_runtime_side_effects_dispatch_through_operation_service() -> None:
    """Runner-only lock: product services do not bypass RuntimeOperationService."""
    offenders = _collect_runtime_side_effect_bypasses()

    assert not offenders, (
        "Product routers/services must dispatch runtime side effects through "
        "RuntimeOperationService, not select providers or call provider operations "
        "directly. Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_product_local_placement_policy_is_not_duplicated_in_product_callers() -> None:
    """Runner-only lock: product/local placement decisions live in product_policy."""
    offenders = _collect_product_local_policy_duplicates()

    assert not offenders, (
        "Product local-placement policy must stay centralized in "
        "`backend/services/runtime_provider/product_policy.py`. Found duplicated "
        "predicates:\n  - "
        + "\n  - ".join(offenders)
    )


def test_local_provider_tests_are_dev_test_scoped_and_explicitly_allowlisted() -> None:
    """Runner-only lock: direct local Docker tests are dev/test provider exceptions."""
    importers = _collect_direct_local_provider_test_importers()
    allowlist = set(_LOCAL_PROVIDER_DEV_TEST_ALLOWLIST)
    disallowed = sorted(importers - allowlist)
    stale = sorted(allowlist - importers)

    assert not disallowed, (
        "Tests importing direct local runtime authority must be explicit "
        "dev/test/provider exceptions, not product execution proof. Found:\n  - "
        + "\n  - ".join(disallowed)
    )
    assert not stale, (
        "Local provider dev/test allowlist contains stale entries:\n  - "
        + "\n  - ".join(
            f"{path} ({_LOCAL_PROVIDER_DEV_TEST_ALLOWLIST[path]})"
            for path in stale
        )
    )

    for rel_path in sorted(allowlist):
        docstring = _module_docstring(_REPO_ROOT / rel_path).lower()
        assert "dev/test" in docstring, f"{rel_path} must declare dev/test scope."
        assert "not product task" in docstring and "execution proof" in docstring, (
            f"{rel_path} must say local Docker coverage is not product proof."
        )
        assert "runner placement" in docstring, (
            f"{rel_path} must point product task runtime at runner placement."
        )


def test_active_terminal_io_uses_manager_provider_path() -> None:
    """Tenant baseline lock: active PTY paths do not call TerminalSession.read/write."""
    offenders: list[str] = []
    for rel_path in _ACTIVE_TERMINAL_IO_MODULES:
        offenders.extend(
            _match_lines(_REPO_ROOT / rel_path, _FORBIDDEN_TERMINAL_SESSION_IO)
        )

    assert not offenders, (
        "Active terminal/PTY paths must route I/O through TerminalSessionManager "
        "provider methods, not TerminalSession.read/write. Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_runtime_artifact_reads_do_not_reconstruct_task_workspace_paths() -> None:
    """Tenant baseline lock: artifact/provenance/archive reads do not rebuild task workspace paths."""
    offenders: list[str] = []
    for rel_path in _RUNTIME_ARTIFACT_READ_MODULES:
        offenders.extend(
            _match_lines(_REPO_ROOT / rel_path, _FORBIDDEN_TASK_WORKSPACE_RECONSTRUCTION)
        )

    assert not offenders, (
        "Runtime artifact readers must use provider artifact reads instead of "
        "reconstructing task workspaces. Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_scope_and_file_routes_do_not_reconstruct_runtime_workspace_paths() -> None:
    """Tenant baseline lock: scope/file routes must use workspace query boundary services."""
    offenders: list[str] = []
    for rel_path in _ROUTE_WORKSPACE_RECONSTRUCTION_MODULES:
        offenders.extend(
            _match_lines(_REPO_ROOT / rel_path, _FORBIDDEN_ROUTE_WORKSPACE_RECONSTRUCTION)
        )

    assert not offenders, (
        "Scope/file routes must not materialize provider workspaces or reconstruct "
        "workspace paths directly. Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_runner_snapshot_normalization_removes_absolute_path_metadata() -> None:
    """Remote runtime lock: snapshot normalization strips runner host absolute-path metadata."""
    text = _RUNTIME_SNAPSHOT_NORMALIZER_PATH.read_text(encoding="utf-8")

    assert "_PATH_KEY_SUFFIX" in text
    assert "endswith(_PATH_KEY_SUFFIX)" in text
    assert "normalized.startswith(\"/\")" in text
    assert "_WINDOWS_DRIVE_PATH_RE" in text


def test_tenant_baseline_boundary_allowlist_is_explicit_and_todo_tagged() -> None:
    """Tenant baseline lock: each residual exception is explicit and owned by a future phase."""
    for entry in _ALLOWLISTED_EXCEPTIONS:
        assert entry.todo.startswith("TODO(")
        assert entry.owner
        assert _FUTURE_OWNER_RE.fullmatch(entry.future_owner), (
            "Allowlist exception must declare a future domain owner (tooling_plane+)."
        )
        assert "execution_plane" not in entry.todo
        assert "execution_plane" not in entry.owner
        assert "execution_plane" not in entry.removal_condition
        assert entry.removal_condition


def test_graph_tool_dispatch_uses_provider_dispatch_boundary() -> None:
    """Tooling plane lock: active batch flow is per-call dispatch through BatchExecutor."""
    path = _REPO_ROOT / "agent/graph/subgraphs/tool_execution_runtime/orchestrator.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("async def _run_batch_tool_execution(")
    end = text.index("async def approval_gate_node_orchestrator(")
    active_batch_flow = text[start:end]

    assert "BatchExecutor().execute" in active_batch_flow
    assert "run_one_call=run_one_call" in active_batch_flow
    assert "await _dispatch_tool_execution_via_provider(" not in active_batch_flow
    assert "provider.dispatch_tool_execution(request)" not in active_batch_flow


def test_runner_control_management_plane_doc_uses_wired_runner_provider_selection_flow() -> None:
    """Runner control lock: management-plane compatibility docs use concrete provider selection names."""
    text = _MANAGEMENT_PLANE_DOC_PATH.read_text(encoding="utf-8")

    assert "-> RunnerRuntimeProvider ->" not in text
    assert "RuntimeProviderRegistry (runner mode)" in text
    assert "build_runner_runtime_provider" in text
    assert "CloudRunnerRuntimeProvider (current managed-runner implementation class)" in text
    assert "StandaloneRunnerRuntimeProvider" not in text


def test_remote_runtime_runner_channel_doc_claims_match_router_and_runner_cli_surfaces() -> None:
    """Remote runtime lock: docs keep endpoint/CLI/config and runtime-operation claims aligned."""
    doc_text = _RUNNER_CHANNEL_DOC_PATH.read_text(encoding="utf-8")
    router_text = _RUNNER_CONTROL_ROUTER_PATH.read_text(encoding="utf-8")
    runner_app_text = _RUNNER_APP_PATH.read_text(encoding="utf-8")
    runner_config_text = _RUNNER_CONFIG_PATH.read_text(encoding="utf-8")

    assert 'POST /api/runner-control/register' in doc_text
    assert 'WS   /api/runner-control/channel' in doc_text
    assert '@router.post("/register"' in router_text
    assert '@router.websocket("/channel")' in router_text

    assert "drowai_runner run" in doc_text
    assert 'add_parser("run"' in runner_app_text
    assert 'if args.command == "run":' in runner_app_text
    assert "drowai_runner cloud-run" not in doc_text
    assert "task.start" in doc_text
    assert "runtime.started" in doc_text
    assert "terminal.result" in doc_text
    assert "tool.command" in doc_text
    assert "artifact.manifest" in doc_text
    assert "artifact.upload.request" in doc_text
    assert "artifact.upload.complete" in doc_text

    for env_var in (
        "DROWAI_RUNNER_CONFIG",
        "DROWAI_RUNNER_ROOT",
        "DROWAI_RUNNER_HOST_BIND_ROOT",
        "DROWAI_RUNNER_HEARTBEAT_INTERVAL_SECONDS",
        "DROWAI_RUNNER_TLS_VERIFY",
    ):
        assert env_var in doc_text

    for env_var in (
        "DROWAI_RUNNER_CONTROL_PLANE_URL",
        "DROWAI_RUNNER_REGISTRATION_TOKEN",
        "DROWAI_RUNNER_TENANT_ID",
        "DROWAI_RUNNER_ID",
        "DROWAI_RUNNER_CREDENTIAL_SECRET_PATH",
        "DROWAI_RUNNER_HEARTBEAT_INTERVAL_SECONDS",
        "DROWAI_RUNNER_TLS_VERIFY",
    ):
        assert env_var in runner_config_text

    assert "DROWAI_RUNNER_REGISTRATION_TOKEN" not in doc_text
    assert "DROWAI_RUNNER_TENANT_ID" not in doc_text
    assert "Raw registration token" in doc_text
    assert "primary product deployment contract" in doc_text

    # Removed compatibility aliases must stay out of docs and runner config.
    assert "DROWAI_RUNNER_MODE" not in doc_text
    assert "DROWAI_RUNNER_CLOUD_BASE_URL" not in doc_text
    assert "DROWAI_RUNNER_MODE" not in runner_config_text
    assert "DROWAI_RUNNER_CLOUD_BASE_URL" in runner_config_text
