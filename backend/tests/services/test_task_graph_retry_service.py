"""Tests for task-scoped checkpoint retry orchestration service.

These tests verify that retry requests are authorized, bound to retryable
failed workflows, and scheduled with checkpoint retry semantics.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    CheckpointRetryClaimResult,
    build_checkpoint_retry_identity,
)
from backend.services.task.graph_retry_service import TaskGraphRetryService


def _owned_task(task_id: int, user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        user_id=user_id,
        tenant_id=77,
        graph_thread_id="a" * 32,
        workspace_id=f"task-{task_id}",
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
    )


@pytest.mark.asyncio
async def test_retry_graph_execution_enqueues_checkpoint_retry_for_retryable_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Mock()
    workflow = SimpleNamespace(
        id=14,
        graph_name="simple_tool",
        turn_id="task-1-turn-3",
        reserved_message_id=33,
        turn_sequence=3,
        state="RETRYING",
        checkpoint_id="ckpt-stable-abc123",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
        },
    )
    identity = build_checkpoint_retry_identity(
        workflow,
        task_id=1,
        already_in_flight=False,
    )
    workflow_service = Mock()
    workflow_service.claim_checkpoint_retry.return_value = CheckpointRetryClaimResult(
        status="claimed",
        workflow=workflow,
        identity=identity,
    )

    scheduled: list[object] = []
    lifecycle_events: list[dict[str, object]] = []

    async def _capture_lifecycle_publish(self: object, state: str, **kwargs: object) -> None:
        lifecycle_events.append({"state": state, **kwargs})

    monkeypatch.setattr(
        "backend.services.task.graph_retry_service.RetryLifecyclePublisher.publish",
        _capture_lifecycle_publish,
    )

    with patch("backend.services.task.graph_retry_service.get_owned_task_or_404") as owned_task_mock, patch(
        "backend.services.task.graph_retry_service.TurnWorkflowService",
        return_value=workflow_service,
    ):
        owned_task_mock.return_value = _owned_task(task_id=1, user_id=99)
        result = await TaskGraphRetryService(db).retry_graph_execution(
            task_id=1,
            user_id=99,
            tenant_id=77,
            turn_id="task-1-turn-3",
            retry_mode="checkpoint",
            graph_name=None,
            create_task_fn=lambda job: scheduled.append(job),
            run_checkpoint_retry_generation=lambda **kwargs: kwargs,
        )

    owned_task_mock.assert_called_once_with(db=db, task_id=1, user_id=99, tenant_id=77)
    # The route must consume the atomic CAS claim primitive.
    workflow_service.claim_checkpoint_retry.assert_called_once()
    claim_kwargs = workflow_service.claim_checkpoint_retry.call_args.kwargs
    assert claim_kwargs.get("task_id") == 1
    assert claim_kwargs.get("turn_id") == "task-1-turn-3"
    assert claim_kwargs.get("graph_name") is None

    assert len(scheduled) == 1
    payload = scheduled[0]
    assert payload["task_id"] == 1
    assert payload["user_id"] == 99
    assert payload["tenant_id"] == 77
    assert payload["runtime_placement_mode"] == "local"
    assert payload["workspace_id"] == "task-1"
    assert payload["actor_type"] == "user"
    assert payload["actor_id"] == "99"
    assert payload["runner_id"] is None
    assert payload["execution_site_id"] is None
    assert payload["workflow_id"] == 14
    assert payload["turn_id"] == "task-1-turn-3"
    assert payload["turn_sequence"] == 3
    assert payload["graph_name"] == "simple_tool"
    assert payload["reserved_message_id"] == 33
    assert payload["checkpoint_id"] == "ckpt-stable-abc123"
    assert payload["retry_attempt"] == 1
    assert payload["retry_max_attempts"] == 2

    # The accepted retry response must carry the full retry identity so the
    # frontend never falls back to placeholder values.
    assert result["status"] == "retrying"
    assert result["task_id"] == 1
    assert result["turn_id"] == "task-1-turn-3"
    assert result["retry_mode"] == "checkpoint"
    assert result["already_in_flight"] is False
    assert result["workflow_id"] == 14
    assert result["checkpoint_id"] == "ckpt-stable-abc123"
    assert result["retry_attempt"] == 1
    assert result["retry_max_attempts"] == 2
    assert result["graph_name"] == "simple_tool"
    assert result["state"] == "retrying"
    assert isinstance(result["identity"], dict)
    assert result["identity"]["workflow_id"] == 14
    assert result["identity"]["checkpoint_id"] == "ckpt-stable-abc123"
    assert lifecycle_events == [{"state": "accepted"}]


@pytest.mark.asyncio
async def test_retry_graph_execution_publishes_failed_lifecycle_when_enqueue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed retry that cannot enqueue must publish a terminal lifecycle."""
    db = Mock()
    workflow = SimpleNamespace(
        id=14,
        graph_name="simple_tool",
        turn_id="task-1-turn-3",
        reserved_message_id=33,
        turn_sequence=3,
        state="RETRYING",
        checkpoint_id="ckpt-stable-abc123",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
        },
    )
    identity = build_checkpoint_retry_identity(
        workflow,
        task_id=1,
        already_in_flight=False,
    )
    workflow_service = Mock()
    workflow_service.claim_checkpoint_retry.return_value = CheckpointRetryClaimResult(
        status="claimed",
        workflow=workflow,
        identity=identity,
    )

    lifecycle_events: list[dict[str, object]] = []

    async def _capture_lifecycle_publish(self: object, state: str, **kwargs: object) -> None:
        lifecycle_events.append({"state": state, **kwargs})

    monkeypatch.setattr(
        "backend.services.task.graph_retry_service.RetryLifecyclePublisher.publish",
        _capture_lifecycle_publish,
    )

    def _raise_enqueue(_job: object) -> None:
        raise RuntimeError("queue unavailable")

    with patch("backend.services.task.graph_retry_service.get_owned_task_or_404") as owned_task_mock, patch(
        "backend.services.task.graph_retry_service.TurnWorkflowService",
        return_value=workflow_service,
    ):
        owned_task_mock.return_value = _owned_task(task_id=1, user_id=99)
        with pytest.raises(RuntimeError, match="queue unavailable"):
            await TaskGraphRetryService(db).retry_graph_execution(
                task_id=1,
                user_id=99,
                tenant_id=77,
                turn_id="task-1-turn-3",
                retry_mode="checkpoint",
                graph_name=None,
                create_task_fn=_raise_enqueue,
                run_checkpoint_retry_generation=lambda **kwargs: kwargs,
            )

    workflow_service.mark_failed.assert_called_once()
    failed_metadata = workflow_service.mark_failed.call_args.kwargs["metadata"]
    assert failed_metadata["error"] == "retry_enqueue_failed"
    assert lifecycle_events == [
        {"state": "accepted"},
        {
            "state": "failed",
            "transcript_resync_required": True,
            "failure_stage": "enqueue",
            "error_code": "retry_enqueue_failed",
        },
    ]


@pytest.mark.asyncio
async def test_retry_graph_execution_threads_checkpoint_id_and_retry_context_to_worker() -> None:
    """Retry worker receives checkpoint identity and sanitized context.

    The retry route must hand the background worker:
      * the persisted ``checkpoint_id`` from the workflow row,
      * the canonical retry identity (``retry_attempt`` + ``retry_max_attempts``),
      * sanitized previous-failure context so graph continuation can choose a
        corrected path instead of blindly replaying the same failing step.
    """
    db = Mock()
    last_failure = {
        "error_code": "tool_argument_invalid",
        "failure_stage": "graph_continuation",
        "graph_name": "simple_tool",
        "tool_name": "http_get",
        "tool_call_id": "call-77",
        "summary": "tool rejected malformed url argument",
    }
    # After a successful CAS-style claim the row is RETRYING with attempt=1
    # and the same checkpoint_id is preserved on the workflow row.
    post_claim_workflow = SimpleNamespace(
        id=14,
        graph_name="simple_tool",
        turn_id="task-1-turn-3",
        reserved_message_id=33,
        turn_sequence=3,
        state="RETRYING",
        checkpoint_id="ckpt-stable-abc123",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
            "last_failure": last_failure,
        },
    )

    identity = build_checkpoint_retry_identity(
        post_claim_workflow,
        task_id=1,
        already_in_flight=False,
    )
    workflow_service = Mock()
    workflow_service.claim_checkpoint_retry.return_value = CheckpointRetryClaimResult(
        status="claimed",
        workflow=post_claim_workflow,
        identity=identity,
    )

    scheduled: list[object] = []

    with patch("backend.services.task.graph_retry_service.get_owned_task_or_404") as owned_task_mock, patch(
        "backend.services.task.graph_retry_service.TurnWorkflowService",
        return_value=workflow_service,
    ):
        owned_task_mock.return_value = _owned_task(task_id=1, user_id=99)
        await TaskGraphRetryService(db).retry_graph_execution(
            task_id=1,
            user_id=99,
            tenant_id=77,
            turn_id="task-1-turn-3",
            retry_mode="checkpoint",
            graph_name=None,
            create_task_fn=lambda job: scheduled.append(job),
            run_checkpoint_retry_generation=lambda **kwargs: kwargs,
        )

    assert len(scheduled) == 1, "exactly one retry worker must be scheduled"
    payload = scheduled[0]
    assert isinstance(payload, dict), "scheduled worker payload must be the kwargs dict"

    # Identity / structural fields are always threaded into the worker carrier.
    assert payload.get("task_id") == 1
    assert payload.get("user_id") == 99
    assert payload.get("tenant_id") == 77
    assert payload.get("runtime_placement_mode") == "local"
    assert payload.get("workspace_id") == "task-1"
    assert payload.get("actor_type") == "user"
    assert payload.get("actor_id") == "99"
    assert payload.get("runner_id") is None
    assert payload.get("execution_site_id") is None
    assert payload.get("workflow_id") == 14
    assert payload.get("turn_id") == "task-1-turn-3"
    assert payload.get("turn_sequence") == 3
    assert payload.get("graph_name") == "simple_tool"
    assert payload.get("reserved_message_id") == 33

    # Checkpoint identity and sanitized failure context are part of the worker
    # carrier; the worker must not recalculate them later.
    assert payload.get("checkpoint_id") == "ckpt-stable-abc123", (
        "retry worker must receive the workflow's stored checkpoint_id; "
        "otherwise it could fall back to an implicit latest checkpoint."
    )
    assert payload.get("retry_attempt") == 1, (
        "retry worker must receive the canonical retry attempt assigned by the "
        "atomic workflow claim, not recompute it later."
    )
    assert payload.get("retry_max_attempts") == 2, (
        "retry worker must receive the backend-owned retry ceiling so retry "
        "context downstream can settle on retry_exhausted instead of looping."
    )
    previous_failure = payload.get("previous_failure")
    assert isinstance(previous_failure, dict) and previous_failure, (
        "retry worker must receive sanitized previous-failure context so the "
        "graph continuation can choose a corrected/alternate path."
    )
    assert previous_failure.get("error_code") == "tool_argument_invalid"
    assert previous_failure.get("failure_stage") == "graph_continuation"
    assert previous_failure.get("tool_name") == "http_get"
    assert previous_failure.get("tool_call_id") == "call-77"
    assert previous_failure.get("summary") == "tool rejected malformed url argument"


@pytest.mark.asyncio
async def test_retry_graph_execution_returns_already_in_flight_for_duplicate_retry_spam() -> None:
    """Duplicate retry clicks return the active retry identity.

    A user (or a flaky frontend) can fire the retry POST twice in quick
    succession. The route should treat the second call as idempotent state
    sync, not a destructive error. This test verifies:
      * the first call schedules exactly one background worker,
      * the second call schedules **zero** additional workers,
      * the second call returns a non-error payload carrying the canonical
        retry identity with ``already_in_flight=True``.
    """
    db = Mock()

    retrying_workflow = SimpleNamespace(
        id=14,
        graph_name="simple_tool",
        turn_id="task-1-turn-3",
        reserved_message_id=33,
        turn_sequence=3,
        state="RETRYING",
        checkpoint_id="ckpt-stable-abc123",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 1,
            "retry_max_attempts": 2,
        },
    )

    claimed_identity = build_checkpoint_retry_identity(
        retrying_workflow,
        task_id=1,
        already_in_flight=False,
    )
    already_identity = build_checkpoint_retry_identity(
        retrying_workflow,
        task_id=1,
        already_in_flight=True,
    )
    workflow_service = Mock()
    # First call: the CAS claim flips FAILED -> RETRYING. Second call:
    # the row is already RETRYING and the claim primitive must report
    # ``already_retrying`` so the route returns the in-flight identity
    # instead of 409ing.
    workflow_service.claim_checkpoint_retry.side_effect = [
        CheckpointRetryClaimResult(
            status="claimed",
            workflow=retrying_workflow,
            identity=claimed_identity,
        ),
        CheckpointRetryClaimResult(
            status="already_retrying",
            workflow=retrying_workflow,
            identity=already_identity,
            detail="Retry already in flight for this turn.",
        ),
    ]

    scheduled: list[object] = []

    def fake_create_task(job: object) -> None:
        scheduled.append(job)

    with patch("backend.services.task.graph_retry_service.get_owned_task_or_404") as owned_task_mock, patch(
        "backend.services.task.graph_retry_service.TurnWorkflowService",
        return_value=workflow_service,
    ):
        owned_task_mock.return_value = _owned_task(task_id=1, user_id=99)
        service = TaskGraphRetryService(db)
        first_response = await service.retry_graph_execution(
            task_id=1,
            user_id=99,
            tenant_id=77,
            turn_id="task-1-turn-3",
            retry_mode="checkpoint",
            graph_name=None,
            create_task_fn=fake_create_task,
            run_checkpoint_retry_generation=lambda **kwargs: kwargs,
        )

        # Second call simulates the duplicate-spam race. It must return a
        # non-error payload with already_in_flight=True.
        try:
            second_response = await service.retry_graph_execution(
                task_id=1,
                user_id=99,
                tenant_id=77,
                turn_id="task-1-turn-3",
                retry_mode="checkpoint",
                graph_name=None,
                create_task_fn=fake_create_task,
                run_checkpoint_retry_generation=lambda **kwargs: kwargs,
            )
        except HTTPException as exc:  # pragma: no cover — failing branch
            pytest.fail(
                "Duplicate retry spam must return an already_in_flight payload, "
                f"not raise HTTPException({exc.status_code}, {exc.detail!r})."
            )

    # First call must have scheduled exactly one background worker.
    assert isinstance(first_response, dict)
    assert first_response.get("status") == "retrying"

    # Duplicate spam path must not schedule a second worker.
    assert len(scheduled) == 1, (
        "duplicate retry spam must not schedule a second background worker; "
        f"got {len(scheduled)} scheduled jobs."
    )

    # The duplicate response must carry the canonical retry identity flagged
    # as already_in_flight so the frontend treats it as state sync, not as
    # a destructive error.
    assert isinstance(second_response, dict), "second retry response must be a payload dict"
    assert second_response.get("already_in_flight") is True, (
        "duplicate retry response must include already_in_flight=True so the "
        "frontend can render existing retry state instead of an error toast."
    )
    assert second_response.get("status") == "retrying"
    assert second_response.get("task_id") == 1
    assert second_response.get("turn_id") == "task-1-turn-3"
    assert second_response.get("retry_mode") == "checkpoint"
    assert second_response.get("workflow_id") == 14, (
        "duplicate retry response must echo the active workflow_id so the "
        "frontend can correlate it with the in-flight retry."
    )
    assert second_response.get("checkpoint_id") == "ckpt-stable-abc123", (
        "duplicate retry response must echo the stored checkpoint_id of the "
        "in-flight retry."
    )
    assert second_response.get("retry_attempt") == 1, (
        "duplicate retry response must echo the canonical retry_attempt of "
        "the in-flight retry, not recompute it."
    )
    assert second_response.get("retry_max_attempts") == 2, (
        "duplicate retry response must echo the backend-owned retry ceiling "
        "so the frontend doesn't enforce its own."
    )


@pytest.mark.asyncio
async def test_retry_graph_execution_refuses_claim_when_retry_budget_is_exhausted() -> None:
    """Exhausted retry budget does not schedule another worker.

    When the workflow row has ``retry_attempt_count >= retry_max_attempts`` the
    claim must refuse to start another retry and the route must:

      * NOT schedule a background worker (no retry loop),
      * return a response that does not advertise a fresh retry CTA
        (i.e. ``retryable=false`` or a typed ``retry_exhausted`` marker),
      * preserve enough sanitized failure context for the transcript to
        render a terminal "out of retries" diagnostic.
    """
    db = Mock()

    # Pin a workflow that is retryable in principle but has already used
    # every attempt in its budget.
    exhausted_workflow = SimpleNamespace(
        id=14,
        graph_name="simple_tool",
        turn_id="task-1-turn-3",
        reserved_message_id=33,
        turn_sequence=3,
        state="FAILED",
        checkpoint_id="ckpt-stable-abc123",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 2,
            "retry_max_attempts": 2,
            "last_failure": {
                "error_code": "tool_argument_invalid",
                "failure_stage": "graph_continuation",
                "graph_name": "simple_tool",
                "tool_name": "http_get",
                "tool_call_id": "call-77",
                "summary": "tool rejected malformed url argument",
            },
        },
    )

    workflow_service = Mock()
    # ``claim_checkpoint_retry`` returns a typed ``retry_exhausted`` result
    # when the budget is spent and never mutates the workflow row. The retry
    # route must surface that terminal result without scheduling another
    # worker.
    workflow_service.claim_checkpoint_retry.return_value = CheckpointRetryClaimResult(
        status="retry_exhausted",
        workflow=exhausted_workflow,
        detail=(
            "Checkpoint retry budget is exhausted "
            "(retry_attempt_count=2, retry_max_attempts=2)."
        ),
    )
    scheduled: list[object] = []

    with patch("backend.services.task.graph_retry_service.get_owned_task_or_404") as owned_task_mock, patch(
        "backend.services.task.graph_retry_service.TurnWorkflowService",
        return_value=workflow_service,
    ):
        owned_task_mock.return_value = _owned_task(task_id=1, user_id=99)
        service = TaskGraphRetryService(db)

        captured_response: dict[str, object] | None = None
        captured_exception: HTTPException | None = None
        try:
            captured_response = await service.retry_graph_execution(
                task_id=1,
                user_id=99,
                tenant_id=77,
                turn_id="task-1-turn-3",
                retry_mode="checkpoint",
                graph_name=None,
                create_task_fn=lambda job: scheduled.append(job),
                run_checkpoint_retry_generation=lambda **kwargs: kwargs,
            )
        except HTTPException as exc:
            captured_exception = exc

    # The retry route must not schedule another worker on an exhausted
    # workflow.
    assert scheduled == [], (
        "exhausted retry budget must not schedule a background worker; "
        f"scheduled={scheduled!r}."
    )

    # Either the route raises a typed terminal error, OR it returns a
    # payload that flags the retry as exhausted. Both shapes satisfy the
    # contract; either way the response must NOT advertise a fresh CTA.
    if captured_exception is not None:
        # Typed terminal: must not be a generic 500.
        assert captured_exception.status_code in (409, 410, 422), (
            "retry-budget-exhausted must surface as a terminal client error, "
            f"got status_code={captured_exception.status_code}."
        )
        # Detail must mention exhaustion, not a generic missing-workflow.
        detail = str(captured_exception.detail or "").lower()
        assert "exhaust" in detail or "retry_exhausted" in detail or "no retries" in detail, (
            "exhausted-budget error detail must describe retry exhaustion so "
            f"the frontend can render a terminal state; got detail={captured_exception.detail!r}."
        )
    else:
        assert isinstance(captured_response, dict), (
            "retry-budget-exhausted must surface as either a typed HTTPException "
            "or a non-error payload describing retry exhaustion."
        )
        # The payload must explicitly mark this as a terminal state and
        # must NOT advertise another retry CTA to the frontend.
        retry_exhausted = (
            captured_response.get("retry_exhausted") is True
            or captured_response.get("status") == "retry_exhausted"
            or captured_response.get("state") == "retry_exhausted"
        )
        assert retry_exhausted, (
            "exhausted-budget response must carry a retry_exhausted marker so "
            f"the UI does not show another retry button; got {captured_response!r}."
        )
        # And it must not say retryable=true on the resulting state.
        assert captured_response.get("retryable") is not True, (
            "exhausted-budget response must not advertise retryable=true; "
            f"got {captured_response!r}."
        )


@pytest.mark.asyncio
async def test_retry_continuation_carrier_preserves_sanitized_previous_failure_only() -> None:
    """Retry continuation carrier preserves sanitized failure context only.

    The background worker receives a small, sanitized previous-failure bundle
    so graph continuation can distinguish a retry from a normal continuation
    and pick a corrected path. Critically, the sanitized carrier must NOT leak:
      * raw provider payloads,
      * authorization headers, cookies, JWTs, API keys,
      * full tool request/response bodies.

    This test stuffs a workflow row with secret-bearing fields under
    ``last_failure.raw_response``/``raw_request``/``headers`` and asserts
    the worker carrier only carries the sanitized fields the contract
    enumerates (``error_code``, ``failure_stage``, ``graph_name``,
    ``tool_name``, ``tool_call_id``, ``summary``) and never leaks the
    secret-bearing keys.
    """
    db = Mock()
    pre_claim_workflow = SimpleNamespace(
        id=14,
        graph_name="simple_tool",
        turn_id="task-1-turn-3",
        reserved_message_id=33,
        turn_sequence=3,
        state="FAILED",
        checkpoint_id="ckpt-stable-abc123",
        workflow_metadata={
            "retryable": True,
            "retry_mode": "checkpoint",
            "retry_attempt_count": 0,
            "retry_max_attempts": 2,
            "last_failure": {
                # Sanitized fields the carrier MUST forward.
                "error_code": "tool_argument_invalid",
                "failure_stage": "graph_continuation",
                "graph_name": "simple_tool",
                "tool_name": "http_get",
                "tool_call_id": "call-77",
                "summary": "tool rejected malformed url argument",
                # Secret-bearing fields the carrier MUST NOT forward.
                "raw_request": {
                    "headers": {
                        "Authorization": "Bearer sk-test-DO-NOT-LEAK",
                        "Cookie": "session=DO-NOT-LEAK",
                    },
                    "body": {"prompt": "ignore previous", "api_key": "sk-LEAK-ME"},
                },
                "raw_response": {
                    "set-cookie": "auth=LEAK-ME; HttpOnly",
                    "body": "<html>secret provider response</html>",
                },
                "auth_token": "Bearer sk-LEAK-ME",
                "api_key": "sk-LEAK-ME",
                "jwt": "eyJ.LEAK.ME",
            },
        },
    )
    post_claim_workflow = SimpleNamespace(
        id=14,
        graph_name="simple_tool",
        turn_id="task-1-turn-3",
        reserved_message_id=33,
        turn_sequence=3,
        state="RETRYING",
        checkpoint_id="ckpt-stable-abc123",
        workflow_metadata=dict(pre_claim_workflow.workflow_metadata),
    )
    post_claim_workflow.workflow_metadata["retry_attempt_count"] = 1

    identity = build_checkpoint_retry_identity(
        post_claim_workflow,
        task_id=1,
        already_in_flight=False,
    )
    workflow_service = Mock()
    workflow_service.claim_checkpoint_retry.return_value = CheckpointRetryClaimResult(
        status="claimed",
        workflow=post_claim_workflow,
        identity=identity,
    )

    scheduled: list[object] = []

    with patch("backend.services.task.graph_retry_service.get_owned_task_or_404") as owned_task_mock, patch(
        "backend.services.task.graph_retry_service.TurnWorkflowService",
        return_value=workflow_service,
    ):
        owned_task_mock.return_value = _owned_task(task_id=1, user_id=99)
        await TaskGraphRetryService(db).retry_graph_execution(
            task_id=1,
            user_id=99,
            tenant_id=77,
            turn_id="task-1-turn-3",
            retry_mode="checkpoint",
            graph_name=None,
            create_task_fn=lambda job: scheduled.append(job),
            run_checkpoint_retry_generation=lambda **kwargs: kwargs,
        )

    assert len(scheduled) == 1, "exactly one retry worker must be scheduled"
    payload = scheduled[0]
    assert isinstance(payload, dict), "scheduled worker payload must be the kwargs dict"

    previous_failure = payload.get("previous_failure")
    assert isinstance(previous_failure, dict) and previous_failure, (
        "retry worker carrier must include sanitized previous_failure "
        "context."
    )

    # Whitelisted sanitized keys must be forwarded verbatim.
    assert previous_failure.get("error_code") == "tool_argument_invalid"
    assert previous_failure.get("failure_stage") == "graph_continuation"
    assert previous_failure.get("tool_name") == "http_get"
    assert previous_failure.get("tool_call_id") == "call-77"
    assert previous_failure.get("summary") == "tool rejected malformed url argument"

    # Secret-bearing keys must NOT appear at all in the carrier — not as
    # nested raw_request/raw_response, not as standalone auth_token/jwt/
    # api_key. Even partial forwarding is a contract break.
    forbidden_top_level_keys = {
        "raw_request",
        "raw_response",
        "auth_token",
        "api_key",
        "jwt",
    }
    leaked_top_level = forbidden_top_level_keys & set(previous_failure.keys())
    assert leaked_top_level == set(), (
        "sanitized previous_failure carrier must not expose secret-bearing "
        f"top-level keys; leaked={sorted(leaked_top_level)!r}."
    )

    # And the serialized carrier must not contain any of the secret literals.
    serialized_carrier = repr(payload)
    for forbidden_literal in (
        "sk-test-DO-NOT-LEAK",
        "DO-NOT-LEAK",
        "sk-LEAK-ME",
        "eyJ.LEAK.ME",
        "LEAK-ME; HttpOnly",
        "secret provider response",
    ):
        assert forbidden_literal not in serialized_carrier, (
            f"retry worker carrier must not leak secret literal "
            f"{forbidden_literal!r}; sanitization must scrub raw payloads."
        )


@pytest.mark.asyncio
async def test_retry_graph_execution_rejects_when_no_retryable_failed_workflow_exists() -> None:
    db = Mock()
    workflow_service = Mock()
    workflow_service.claim_checkpoint_retry.return_value = CheckpointRetryClaimResult(
        status="missing",
        detail="No workflow exists for this turn.",
    )

    with patch("backend.services.task.graph_retry_service.get_owned_task_or_404") as owned_task_mock, patch(
        "backend.services.task.graph_retry_service.TurnWorkflowService",
        return_value=workflow_service,
    ):
        owned_task_mock.return_value = _owned_task(task_id=1, user_id=99)
        with pytest.raises(HTTPException) as exc_info:
            await TaskGraphRetryService(db).retry_graph_execution(
                task_id=1,
                user_id=99,
                tenant_id=77,
                turn_id="task-1-turn-3",
                retry_mode="checkpoint",
                graph_name=None,
                create_task_fn=lambda _job: None,
                run_checkpoint_retry_generation=lambda **kwargs: kwargs,
            )

    assert exc_info.value.status_code == 409
    detail = str(exc_info.value.detail or "").lower()
    # Detail must mention the typed reason so the frontend can branch.
    assert "missing" in detail
