"""tenant_isolation-authorization route boundary guardrails.

Responsibilities:
- Keep tenant-owned routers on tenant-context dependencies.
- Block direct user-owned task/engagement filters unless explicitly allowlisted.
- Require explicit exception reasons for non-tenant route surfaces.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import pathlib


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_TENANT_OWNED_ROUTER_MODULES: tuple[str, ...] = (
    "backend/routers/tasks/crud.py",
    "backend/routers/tasks/files.py",
    "backend/routers/tasks/interrupt_inbox.py",
    "backend/routers/tasks/interrupts.py",
    "backend/routers/tasks/logs.py",
    "backend/routers/tasks/metrics.py",
    "backend/routers/tasks/runtime.py",
    "backend/routers/tasks/scope.py",
    "backend/routers/tasks/container.py",
    "backend/routers/tasks/vpn.py",
    "backend/routers/chat/cancel.py",
    "backend/routers/chat/history.py",
    "backend/routers/chat/prewarm_ready.py",
    "backend/routers/chat/status.py",
    "backend/routers/chat/submit.py",
    "backend/routers/docker_logs_rest.py",
    "backend/routers/docker_terminal_sessions.py",
    "backend/routers/agent_reasoning.py",
    "backend/routers/artifact_provenance.py",
    "backend/routers/engagement_knowledge.py",
    "backend/routers/engagements_crud.py",
    "backend/routers/knowledge.py",
    "backend/routers/llm.py",
    "backend/routers/reports.py",
    "backend/routers/tenants.py",
    "backend/routers/usage.py",
)

_TENANT_CONTEXT_IMPORT_TOKENS: tuple[str, ...] = (
    "get_tenant_request_context",
    "resolve_tenant_context_for_request",
)


@dataclass(frozen=True, slots=True)
class _DirectUserFilterAllowlistEntry:
    path: str
    owner_field: str
    reason: str


_DIRECT_USER_FILTER_ALLOWLIST: tuple[_DirectUserFilterAllowlistEntry, ...] = ()

_NON_TENANT_ROUTE_ALLOWLIST: dict[str, str] = {
    "backend/routers/runner_control.py": (
        "Runner channel auth is credential-bound and validates tenant binding inside "
        "RunnerChannelAuthService, not a user-selected tenant resource read."
    ),
    "backend/routers/settings.py": (
        "Personal provider credentials and user settings are user-owned preferences, "
        "not tenant-owned resources."
    ),
}


def _read(rel_path: str) -> str:
    return (_REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _iter_router_module_paths() -> list[pathlib.Path]:
    router_root = _REPO_ROOT / "backend/routers"
    module_paths: list[pathlib.Path] = []
    for path in sorted(router_root.rglob("*.py")):
        if path.name in {"__init__.py", "router_bundle.py", "schemas.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "APIRouter" not in text or "@router." not in text:
            continue
        module_paths.append(path)
    return module_paths


def _attribute_chain(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _find_direct_user_scope_filters(path: pathlib.Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    matches: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if any(not isinstance(op, ast.Eq) for op in node.ops):
            continue
        comparators = [node.left, *node.comparators]
        for left, right in zip(comparators, comparators[1:]):
            left_chain = _attribute_chain(left)
            right_chain = _attribute_chain(right)
            if left_chain is None or right_chain is None:
                continue
            pairs = ((left_chain, right_chain), (right_chain, left_chain))
            for first, second in pairs:
                if first in {("Task", "user_id"), ("Engagement", "user_id")} and second == (
                    "current_user",
                    "id",
                ):
                    source = ast.get_source_segment(text, node) or "<source unavailable>"
                    matches.append((node.lineno, source.strip()))
                    break
    return matches


def test_tenant_isolation_tenant_owned_routers_require_tenant_context_dependency() -> None:
    missing: list[str] = []
    for rel_path in _TENANT_OWNED_ROUTER_MODULES:
        text = _read(rel_path)
        if any(token in text for token in _TENANT_CONTEXT_IMPORT_TOKENS):
            continue
        missing.append(rel_path)

    assert not missing, (
        "Tenant-owned router modules must wire tenant-context dependencies. Missing:\n  - "
        + "\n  - ".join(missing)
    )


def test_tenant_isolation_direct_user_scope_filters_are_explicitly_allowlisted() -> None:
    allowlist = {(entry.path, entry.owner_field): entry for entry in _DIRECT_USER_FILTER_ALLOWLIST}
    offenders: list[str] = []

    for module_path in _iter_router_module_paths():
        rel_path = str(module_path.relative_to(_REPO_ROOT))
        for line_number, source in _find_direct_user_scope_filters(module_path):
            owner_field = "Task.user_id" if "Task.user_id" in source else "Engagement.user_id"
            entry = allowlist.get((rel_path, owner_field))
            if entry is None:
                offenders.append(f"{rel_path}:{line_number}: {source}")

    assert not offenders, (
        "Direct user-owned task/engagement filters are forbidden unless explicitly allowlisted.\n"
        "Add tenant-context authorization or a reviewed own-resource exception.\n"
        "Found:\n  - "
        + "\n  - ".join(offenders)
    )


def test_tenant_isolation_user_scope_allowlist_reasons_are_explicit() -> None:
    for entry in _DIRECT_USER_FILTER_ALLOWLIST:
        reason = entry.reason.strip().lower()
        assert reason, f"Missing allowlist reason for {entry.path} ({entry.owner_field})"
        assert "own-resource" in reason, (
            "Allowlisted direct user filter entries must document an explicit own-resource policy reason: "
            f"{entry.path} ({entry.owner_field})"
        )


def test_tenant_isolation_non_tenant_route_exceptions_are_documented() -> None:
    for rel_path, reason in _NON_TENANT_ROUTE_ALLOWLIST.items():
        reason_normalized = reason.strip().lower()
        assert reason_normalized, f"Missing non-tenant route reason for {rel_path}"
        assert "not tenant-owned" in reason_normalized or "not a user-selected tenant resource read" in reason_normalized

    runner_control_text = _read("backend/routers/runner_control.py")
    assert '@router.websocket("/channel")' in runner_control_text
    assert "RunnerChannelAuthService" in runner_control_text

    settings_text = _read("backend/routers/settings.py")
    assert "UserSettings.user_id == current_user.id" in settings_text
