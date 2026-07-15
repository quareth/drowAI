"""Tests for runtime provider context resolver identity guarantees.

Responsibilities:
- Ensure runtime-bound contexts fail closed when tenant metadata is missing.
- Ensure internal context resolution can inherit stable user ownership metadata.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.services.runtime_provider import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeProviderContextResolver,
)

GRAPH_THREAD_ID = "a" * 32


def test_context_from_task_fails_closed_when_tenant_id_missing() -> None:
    task = SimpleNamespace(
        id=11,
        user_id=22,
        tenant_id=None,
        graph_thread_id=GRAPH_THREAD_ID,
        workspace_id="task-11",
        runtime_placement_mode="local",
    )

    with pytest.raises(HTTPException) as exc:
        RuntimeProviderContextResolver.context_from_task(task=task, user_id=22)

    assert exc.value.status_code == 500
    assert "tenant_id" in str(exc.value.detail)


def test_context_from_task_defaults_user_id_from_task_owner_for_internal_flows() -> None:
    task = SimpleNamespace(
        id=15,
        user_id=33,
        tenant_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        workspace_id="task-15",
        runtime_placement_mode="local",
    )

    context = RuntimeProviderContextResolver._from_task(
        task=task,
        actor_type=RuntimeActorType.AGENT,
        actor_id="agent_session:canonical",
        user_id=None,
    )

    assert context.tenant_id == 7
    assert context.user_id == 33
    assert context.graph_thread_id == GRAPH_THREAD_ID
    assert context.actor_type == RuntimeActorType.AGENT


def test_context_from_task_rejects_missing_product_runtime_placement() -> None:
    task = SimpleNamespace(
        id=16,
        user_id=33,
        tenant_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        workspace_id="task-16",
    )

    with pytest.raises(HTTPException) as exc:
        RuntimeProviderContextResolver.context_from_task(task=task, user_id=33)

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "reason_code": "MISSING_RUNTIME_PLACEMENT",
        "task_id": 16,
        "scope": "product_task",
        "message": "Product task runtime context requires explicit runtime_placement_mode.",
    }


def test_context_from_task_allows_missing_runtime_placement_for_test_scope() -> None:
    task = SimpleNamespace(
        id=17,
        user_id=33,
        tenant_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        workspace_id="task-17",
    )

    context = RuntimeProviderContextResolver.context_from_task(
        task=task,
        user_id=33,
        runtime_call_scope=RuntimeCallScope.TEST,
    )

    assert context.runtime_placement_mode == "local"
    assert context.runtime_call_scope is RuntimeCallScope.TEST
