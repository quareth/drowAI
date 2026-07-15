"""Tests for task closure memo preparation orchestration."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, User
from backend.models.reporting import TaskClosureMemo
from backend.models.tenant import Tenant
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.reporting.contracts import (
    TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE,
    TASK_MEMO_ERROR_GENERATION_FAILED,
    TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS,
    TASK_MEMO_ERROR_PROMPT_RENDER_FAILED,
    TASK_MEMO_ERROR_TASK_NOT_FOUND,
    TASK_MEMO_ERROR_TASK_NOT_STOPPED,
    TASK_MEMO_ERROR_VALIDATION_FAILED,
)
from backend.services.reporting.memo_generator import TaskClosureMemoGenerationError
from backend.services.reporting.runtime_readiness_service import RuntimeReadiness
from backend.services.reporting.task_memo_service import (
    TaskMemoService,
    TaskMemoServiceError,
)
from backend.services.reporting.validation import (
    TaskClosureMemoValidationError,
    TaskClosureMemoValidationIssue,
)


TASK_MEMO_SERVICE_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    TaskClosureMemo.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=TASK_MEMO_SERVICE_TABLES)
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _seed_scope(session, *, label: str, status: str = TaskStatus.STOPPED.value):
    tenant = Tenant(slug=f"tenant-{label}-{uuid.uuid4().hex}", name=f"Tenant {label}")
    user = User(username=f"user-{label}-{uuid.uuid4().hex}", password="hashed-password")
    session.add_all([tenant, user])
    session.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
    )
    session.add(engagement)
    session.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {label}",
        status=status,
    )
    session.add(task)
    session.flush()
    return tenant, user, engagement, task


def _add_memo(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    version: int,
    is_current: bool = True,
    source_watermark: dict[str, Any] | None = None,
) -> TaskClosureMemo:
    memo = TaskClosureMemo(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        version=version,
        is_current=is_current,
        status="ready",
        memo_mode="supported",
        source_watermark=source_watermark or {"schema_version": 1, "sources": {"turn": version}},
        memo={
            "task_name": "Task",
            "summary": f"memo-{version}",
            "include_in_report_recommendation": {
                "include": True,
                "reason": "Supported by scoped sources.",
            },
            "actions_performed": [],
            "reportable_observations": [],
            "possible_findings": [],
            "limitations": [],
            "unsupported_notes": [],
            "evidence_refs": [],
            "knowledge_refs": [],
        },
    )
    session.add(memo)
    session.flush()
    return memo


def _add_preparing_memo(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    version: int,
    created_at: datetime | None = None,
) -> TaskClosureMemo:
    memo_kwargs: dict[str, Any] = {}
    if created_at is not None:
        memo_kwargs["created_at"] = created_at
        memo_kwargs["updated_at"] = created_at

    memo = TaskClosureMemo(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        version=version,
        is_current=False,
        status="preparing",
        memo_mode="supported",
        source_watermark={"schema_version": 1, "sources": {"turn": version}},
        memo={},
        **memo_kwargs,
    )
    session.add(memo)
    session.flush()
    return memo


def _memo_payload() -> dict[str, Any]:
    return {
        "task_name": "Task",
        "summary": "Prepared memo.",
        "include_in_report_recommendation": {
            "include": True,
            "reason": "Supported by scoped evidence.",
        },
        "actions_performed": [],
        "reportable_observations": [],
        "possible_findings": [],
        "limitations": [],
        "unsupported_notes": [],
        "evidence_refs": [],
        "knowledge_refs": [],
    }


class _Readiness:
    def __init__(self, readiness: RuntimeReadiness) -> None:
        self.readiness = readiness

    def compute_for_task(self, *, tenant_id: int, task_id: int) -> RuntimeReadiness:
        return self.readiness


class _Watermarks:
    def __init__(self, watermark: dict[str, Any]) -> None:
        self.watermark = watermark

    def compute_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
    ) -> dict[str, Any]:
        return dict(self.watermark)


class _ContextBuilder:
    def __init__(self, context: Any) -> None:
        self.context = context

    def build_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        regenerate: bool = False,
    ) -> Any:
        return self.context


class _PromptRenderer:
    def render(self, context: Any) -> SimpleNamespace:
        return SimpleNamespace(metadata={"prompt_version": "v1"})


class _Generator:
    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.payload = payload or _memo_payload()
        self.metadata = metadata or {"provider": "test"}
        self.exc = exc
        self.calls = 0

    async def generate(self, *, user_id: int, task_id: int, rendered_prompt: Any) -> SimpleNamespace:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(payload=self.payload, metadata=self.metadata)


class _Validator:
    def validate(self, *, payload: dict[str, Any], context: Any) -> SimpleNamespace:
        return SimpleNamespace(payload=payload, metadata={"validation_status": "passed"})


class _FailingReadyRepository(TaskClosureMemoRepository):
    def mark_memo_ready(self, **kwargs: Any) -> TaskClosureMemo | None:
        raise RuntimeError("database write failed")


class _IntegrityErrorRepository(TaskClosureMemoRepository):
    def create_memo_attempt(self, **kwargs: Any) -> TaskClosureMemo:
        raise IntegrityError("insert task memo attempt", {}, Exception("unique"))


class _FailingPromptRenderer:
    def render(self, context: Any) -> SimpleNamespace:
        raise RuntimeError("raw prompt: SECRET_PROMPT_TEXT")


class _FailingContextBuilder:
    def build_for_task(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        task_id: int,
        regenerate: bool = False,
    ) -> Any:
        raise ValueError("full transcript: SECRET_TRANSCRIPT_TEXT")


class _FailingValidator:
    def validate(self, *, payload: dict[str, Any], context: Any) -> SimpleNamespace:
        raise TaskClosureMemoValidationError(
            issues=[
                TaskClosureMemoValidationIssue(
                    code="schema_invalid",
                    path="summary",
                    message="Generated memo does not match the required schema.",
                )
            ],
        )


def _ready() -> RuntimeReadiness:
    return RuntimeReadiness(
        runtime_retired=True,
        useful_runtime_execution=True,
        not_preparable_reason=None,
    )


def _not_stopped() -> RuntimeReadiness:
    return RuntimeReadiness(
        runtime_retired=False,
        useful_runtime_execution=True,
        not_preparable_reason=TASK_MEMO_ERROR_TASK_NOT_STOPPED,
    )


def _context(*, watermark: dict[str, Any] | None = None, memo_mode: str = "supported") -> SimpleNamespace:
    return SimpleNamespace(
        is_preparable=True,
        memo_mode=memo_mode,
        not_preparable_reason=None,
        source_watermark=watermark or {"schema_version": 1, "sources": {"turn": 2}},
    )


@pytest.mark.asyncio
async def test_first_supported_prepare_creates_version_1_ready_current_memo() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="first-supported")
        session.commit()
        watermark = {"schema_version": 1, "sources": {"turn": 1}}
        payload = _memo_payload()
        generator = _Generator(
            payload=payload,
            metadata={
                "provider": "test",
                "model": "memo-model",
                "duration_ms": 11,
            },
        )
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks(watermark),
            context_builder=_ContextBuilder(_context(watermark=watermark)),
            prompt_renderer=_PromptRenderer(),
            generator=generator,
            validator=_Validator(),
        )

        result = await service.prepare_task_memo(
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
        )

        stored = session.query(TaskClosureMemo).one()
        assert result.id == stored.id
        assert result.version == 1
        assert result.status == "ready"
        assert result.is_current is True
        assert result.memo_mode == "supported"
        assert result.source_watermark == watermark
        assert result.memo == payload
        assert result.generation_metadata == {
            "provider": "test",
            "model": "memo-model",
            "duration_ms": 11,
            "validation_status": "passed",
            "source_watermark_schema_version": 1,
        }
        assert result.error_message is None
        assert result.generated_at is not None
        assert generator.calls == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_first_limited_prepare_creates_version_1_ready_current_memo_without_claims() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="first-limited")
        session.commit()
        watermark = {"schema_version": 1, "sources": {"turn": 1}}
        payload = {
            **_memo_payload(),
            "summary": "Prepared limited memo.",
            "reportable_observations": [],
            "possible_findings": [],
        }
        generator = _Generator(
            payload=payload,
            metadata={
                "provider": "test",
                "model": "memo-model",
                "duration_ms": 13,
            },
        )
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks(watermark),
            context_builder=_ContextBuilder(_context(watermark=watermark, memo_mode="limited")),
            prompt_renderer=_PromptRenderer(),
            generator=generator,
            validator=_Validator(),
        )

        result = await service.prepare_task_memo(
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
        )

        stored = session.query(TaskClosureMemo).one()
        assert result.id == stored.id
        assert result.version == 1
        assert result.status == "ready"
        assert result.is_current is True
        assert result.memo_mode == "limited"
        assert result.source_watermark == watermark
        assert result.memo == payload
        assert result.memo["reportable_observations"] == []
        assert result.memo["possible_findings"] == []
        assert result.generation_metadata == {
            "provider": "test",
            "model": "memo-model",
            "duration_ms": 13,
            "validation_status": "passed",
            "source_watermark_schema_version": 1,
        }
        assert result.error_message is None
        assert result.generated_at is not None
        assert generator.calls == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_non_owned_task_raises_not_found_without_attempt() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="owner")
        _other_tenant, other_user, _other_engagement, _other_task = _seed_scope(
            session,
            label="other",
        )
        session.commit()

        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(),
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=other_user.id,
                task_id=task.id,
            )

        assert exc.value.reason == TASK_MEMO_ERROR_TASK_NOT_FOUND
        assert session.query(TaskClosureMemo).count() == 0
        assert user.id != other_user.id
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_non_retired_task_rejected_before_attempt() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(
            session,
            label="running",
            status=TaskStatus.RUNNING.value,
        )
        session.commit()
        generator = _Generator()
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_not_stopped()),
            source_watermarks=_Watermarks({"schema_version": 1}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=generator,
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )

        assert exc.value.reason == TASK_MEMO_ERROR_TASK_NOT_STOPPED
        assert generator.calls == 0
        assert session.query(TaskClosureMemo).count() == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_fresh_current_memo_returns_without_generation() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, engagement, task = _seed_scope(session, label="fresh")
        watermark = {"schema_version": 1, "sources": {"turn": 1}}
        current = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
            source_watermark=watermark,
        )
        session.commit()
        generator = _Generator()
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks(watermark),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=generator,
            validator=_Validator(),
        )

        result = await service.prepare_task_memo(
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
        )

        assert result.id == current.id
        assert generator.calls == 0
        assert session.query(TaskClosureMemo).count() == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_active_preparing_attempt_blocks_duplicate_generation() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, engagement, task = _seed_scope(session, label="active-preparing")
        preparing = _add_preparing_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
        )
        session.commit()
        generator = _Generator()
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1, "sources": {"turn": 1}}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=generator,
            validator=_Validator(),
            memo_preparing_stale_timeout_seconds=1800,
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )

        session.refresh(preparing)
        assert exc.value.reason == TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS
        assert exc.value.safe_message == "Task memo preparation is already in progress."
        assert preparing.status == "preparing"
        assert generator.calls == 0
        assert session.query(TaskClosureMemo).count() == 1
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_stale_preparing_attempt_is_failed_before_new_generation() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, engagement, task = _seed_scope(session, label="stale-preparing")
        stale_attempt = _add_preparing_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
            created_at=datetime.now(UTC) - timedelta(seconds=120),
        )
        session.commit()
        generator = _Generator()
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1, "sources": {"turn": 2}}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=generator,
            validator=_Validator(),
            memo_preparing_stale_timeout_seconds=60,
        )

        result = await service.prepare_task_memo(
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
        )

        attempts = (
            session.query(TaskClosureMemo)
            .filter(TaskClosureMemo.task_id == task.id)
            .order_by(TaskClosureMemo.version.asc())
            .all()
        )
        session.refresh(stale_attempt)
        assert result.version == 2
        assert result.status == "ready"
        assert result.is_current is True
        assert generator.calls == 1
        assert [
            (attempt.version, attempt.status, attempt.is_current)
            for attempt in attempts
        ] == [(1, "failed", False), (2, "ready", True)]
        assert (
            stale_attempt.error_message
            == "Task closure memo preparation exceeded the in-flight timeout."
        )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_preparing_insert_integrity_error_maps_to_in_progress() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="race")
        session.commit()
        service = TaskMemoService(
            session,
            repository=_IntegrityErrorRepository(session),
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1, "sources": {"turn": 1}}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(),
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )

        assert exc.value.reason == TASK_MEMO_ERROR_PREPARATION_IN_PROGRESS
        assert exc.value.safe_message == "Task memo preparation is already in progress."
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_regeneration_promotes_new_ready_version_after_validation() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, engagement, task = _seed_scope(session, label="regenerate")
        current = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
        )
        session.commit()
        watermark = {"schema_version": 1, "sources": {"turn": 2}}
        generation_metadata = {
            "prompt_family": "task_closure_memo",
            "prompt_version": "v1",
            "prompt_template_ids": [
                "task_closure_memo_system",
                "task_closure_memo_user",
            ],
            "provider": "test",
            "model": "memo-model",
            "memo_schema_version": "task_closure_memo.v1",
            "duration_ms": 18,
            "usage": {"total_tokens": 33, "api_key": "SECRET_NESTED_API_KEY"},
            "raw_prompt": "SECRET_PROMPT_TEXT",
            "raw_model_output": "SECRET_MODEL_OUTPUT",
        }
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks(watermark),
            context_builder=_ContextBuilder(_context(watermark=watermark, memo_mode="limited")),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(metadata=generation_metadata),
            validator=_Validator(),
        )

        result = await service.prepare_task_memo(
            tenant_id=tenant.id,
            user_id=user.id,
            task_id=task.id,
            regenerate=True,
        )

        session.refresh(current)
        assert result.version == 2
        assert result.status == "ready"
        assert result.is_current is True
        assert result.memo_mode == "limited"
        assert result.source_watermark == watermark
        assert result.generation_metadata["provider"] == "test"
        assert result.generation_metadata["model"] == "memo-model"
        assert result.generation_metadata["prompt_family"] == "task_closure_memo"
        assert result.generation_metadata["prompt_version"] == "v1"
        assert result.generation_metadata["prompt_template_ids"] == [
            "task_closure_memo_system",
            "task_closure_memo_user",
        ]
        assert result.generation_metadata["memo_schema_version"] == (
            "task_closure_memo.v1"
        )
        assert result.generation_metadata["source_watermark_schema_version"] == 1
        assert result.generation_metadata["duration_ms"] == 18
        assert result.generation_metadata["usage"] == {"total_tokens": 33}
        assert result.generation_metadata["validation_status"] == "passed"
        json.dumps(result.generation_metadata)
        assert "SECRET_" not in str(result.generation_metadata)
        assert current.is_current is False
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_failed_regeneration_preserves_previous_current_memo() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, engagement, task = _seed_scope(session, label="failed")
        current = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
        )
        session.commit()
        generation_error = TaskClosureMemoGenerationError(
            reason=TASK_MEMO_ERROR_GENERATION_FAILED,
            safe_message="LLM memo generation failed.",
            metadata={"duration_ms": 12},
        )
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1, "sources": {"turn": 2}}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(exc=generation_error),
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
                regenerate=True,
            )

        attempts = (
            session.query(TaskClosureMemo)
            .filter(TaskClosureMemo.task_id == task.id)
            .order_by(TaskClosureMemo.version.asc())
            .all()
        )
        session.refresh(current)
        assert exc.value.reason == TASK_MEMO_ERROR_GENERATION_FAILED
        assert current.is_current is True
        assert [(attempt.version, attempt.status, attempt.is_current) for attempt in attempts] == [
            (1, "ready", True),
            (2, "failed", False),
        ]
        assert attempts[1].error_message == "LLM memo generation failed."
        assert attempts[1].generation_metadata == {
            "duration_ms": 12,
            "source_watermark_schema_version": 1,
        }
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_failed_generation_persists_safe_attempt_details_and_watermark() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="safe-generation")
        session.commit()
        watermark = {"schema_version": 1, "sources": {"turn": 3}}
        generation_error = TaskClosureMemoGenerationError(
            reason=TASK_MEMO_ERROR_GENERATION_FAILED,
            safe_message="raw prompt SECRET_PROMPT_TEXT raw model SECRET_MODEL_OUTPUT",
            metadata={
                "duration_ms": 14,
                "prompt_family": "task_closure_memo",
                "prompt_version": "v1",
                "prompt_template_ids": [
                    "task_closure_memo_system",
                    "task_closure_memo_user",
                ],
                "provider": "test",
                "model": "memo-model",
                "memo_schema_version": "task_closure_memo.v1",
                "usage": {"total_tokens": 22, "api_key": "SECRET_NESTED_API_KEY"},
                "raw_prompt": "SECRET_PROMPT_TEXT",
                "full_transcript": "SECRET_TRANSCRIPT_TEXT",
                "raw_model_output": "SECRET_MODEL_OUTPUT",
                "api_key": "SECRET_API_KEY",
            },
        )
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks(watermark),
            context_builder=_ContextBuilder(_context(watermark=watermark)),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(exc=generation_error),
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )

        attempt = session.query(TaskClosureMemo).one()
        assert exc.value.reason == TASK_MEMO_ERROR_GENERATION_FAILED
        assert attempt.status == "failed"
        assert attempt.is_current is False
        assert attempt.error_message == "LLM memo generation failed."
        assert attempt.source_watermark == watermark
        assert attempt.generation_metadata == {
            "duration_ms": 14,
            "prompt_family": "task_closure_memo",
            "prompt_version": "v1",
            "prompt_template_ids": [
                "task_closure_memo_system",
                "task_closure_memo_user",
            ],
            "provider": "test",
            "model": "memo-model",
            "memo_schema_version": "task_closure_memo.v1",
            "source_watermark_schema_version": 1,
            "usage": {"total_tokens": 22},
        }
        json.dumps(attempt.generation_metadata)
        serialized_attempt = str(
            {
                "error_message": attempt.error_message,
                "metadata": attempt.generation_metadata,
            }
        )
        assert "SECRET_" not in serialized_attempt
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_prompt_failure_persists_safe_error_without_raw_prompt() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="safe-prompt")
        session.commit()
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_FailingPromptRenderer(),
            generator=_Generator(),
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )

        attempt = session.query(TaskClosureMemo).one()
        assert exc.value.reason == TASK_MEMO_ERROR_PROMPT_RENDER_FAILED
        assert attempt.status == "failed"
        assert attempt.error_message == "Task closure memo prompt could not be rendered."
        assert "SECRET_PROMPT_TEXT" not in attempt.error_message
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_context_failure_persists_safe_error_without_full_transcript() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="safe-context")
        session.commit()
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1}),
            context_builder=_FailingContextBuilder(),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(),
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )

        attempt = session.query(TaskClosureMemo).one()
        assert exc.value.reason == TASK_MEMO_ERROR_CONTEXT_UNAVAILABLE
        assert attempt.status == "failed"
        assert attempt.error_message == "Task closure memo context could not be built."
        assert "SECRET_TRANSCRIPT_TEXT" not in attempt.error_message
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_validation_failure_persists_safe_metadata_without_model_output() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, _engagement, task = _seed_scope(session, label="safe-validation")
        session.commit()
        service = TaskMemoService(
            session,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(
                payload={"summary": "SECRET_MODEL_OUTPUT"},
            ),
            validator=_FailingValidator(),
        )

        with pytest.raises(TaskMemoServiceError) as exc:
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )

        attempt = session.query(TaskClosureMemo).one()
        assert exc.value.reason == TASK_MEMO_ERROR_VALIDATION_FAILED
        assert attempt.status == "failed"
        assert attempt.memo == {}
        assert attempt.error_message == "Generated task closure memo failed validation."
        assert attempt.generation_metadata["validation_status"] == "failed"
        assert attempt.generation_metadata["issue_count"] == 1
        assert "SECRET_MODEL_OUTPUT" not in str(attempt.generation_metadata)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_ready_promotion_rollback_preserves_previous_current_and_marks_failed() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, engagement, task = _seed_scope(session, label="rollback")
        current = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
        )
        session.commit()
        repo = _FailingReadyRepository(session)
        service = TaskMemoService(
            session,
            repository=repo,
            runtime_readiness=_Readiness(_ready()),
            source_watermarks=_Watermarks({"schema_version": 1, "sources": {"turn": 2}}),
            context_builder=_ContextBuilder(_context()),
            prompt_renderer=_PromptRenderer(),
            generator=_Generator(),
            validator=_Validator(),
        )

        with pytest.raises(TaskMemoServiceError):
            await service.prepare_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
                regenerate=True,
            )

        attempts = (
            session.query(TaskClosureMemo)
            .filter(TaskClosureMemo.task_id == task.id)
            .order_by(TaskClosureMemo.version.asc())
            .all()
        )
        session.refresh(current)
        assert current.is_current is True
        assert [(attempt.version, attempt.status, attempt.is_current) for attempt in attempts] == [
            (1, "ready", True),
            (2, "failed", False),
        ]
    finally:
        engine.dispose()


def test_current_and_history_reads_are_scoped_to_task_owner() -> None:
    engine, factory = _make_session_factory()
    try:
        session = factory()
        tenant, user, engagement, task = _seed_scope(session, label="read")
        _other_tenant, other_user, _other_engagement, _other_task = _seed_scope(
            session,
            label="read-other",
        )
        current = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
        )
        _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=2,
            is_current=False,
        )
        session.commit()
        service = TaskMemoService(session)

        assert (
            service.get_current_task_memo(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            ).id
            == current.id
        )
        assert [
            memo.version
            for memo in service.list_task_memo_history(
                tenant_id=tenant.id,
                user_id=user.id,
                task_id=task.id,
            )
        ] == [2, 1]
        with pytest.raises(TaskMemoServiceError) as exc:
            service.get_current_task_memo(
                tenant_id=tenant.id,
                user_id=other_user.id,
                task_id=task.id,
            )
        assert exc.value.reason == TASK_MEMO_ERROR_TASK_NOT_FOUND
    finally:
        engine.dispose()
