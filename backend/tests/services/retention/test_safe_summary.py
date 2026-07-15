"""Guard tests for no-secret retention summaries and audit payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from backend.schemas.retention import RetentionRunResponse
from backend.services.retention.audit import (
    UnsafeAuditPayloadError,
    to_safe_audit_payload,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_TENANT,
    RetentionRunRequest,
)
from backend.services.retention.orchestrator import RetentionOrchestrator
from backend.services.retention.scheduling import RetentionExecutorOrderEntry


UNSAFE_SUMMARY_FIELD_PARTS = (
    "token",
    "secret",
    "cookie",
    "authorization",
    "api_key",
    "prompt",
    "transcript",
    "payload",
    "content",
    "object_key",
)


@dataclass(frozen=True, slots=True)
class _Policy:
    retention_batch_size_per_tenant: int = 25


class _FakeSession:
    def __init__(self) -> None:
        self.rollback_count = 0

    def rollback(self) -> None:
        self.rollback_count += 1

    def commit(self) -> None:
        raise AssertionError("dry-run should not commit")


class _LeakyExecutor:
    name = "runner_control.retention"
    retention_class = RETENTION_CLASS_OPERATIONAL_EPHEMERAL

    def run(
        self,
        *,
        policy: object,
        tenant_id: int,
        mode: str,
        limit: int,
    ) -> dict[str, Any]:
        unsafe_fields = {
            f"raw_{part}": f"leaked-{part}" for part in UNSAFE_SUMMARY_FIELD_PARTS
        }
        return {
            "executor_name": self.name,
            "retention_class": self.retention_class,
            "mode": mode,
            "tenant_id": tenant_id,
            "counts": {
                "scanned_count": 4,
                "candidate_count": 3,
                "protected_count": 1,
                "applied_count": 0,
                "skipped_count": 1,
                "failed_count": 0,
                "batch_count": 4,
                "batch_limit": limit,
                **{f"count_{key}": 99 for key in unsafe_fields},
            },
            "reason_counts": {
                "expired_operational_record": 3,
                "protected_active_record": 1,
            },
            "decisions": (
                {
                    "retention_class": self.retention_class,
                    "outcome": RETENTION_DECISION_CANDIDATE,
                    "reason_code": "expired_operational_record",
                    "resource_id": "runner-session:123",
                    **{
                        f"decision_{part}": f"decision-leak-{part}"
                        for part in UNSAFE_SUMMARY_FIELD_PARTS
                    },
                },
            ),
            **unsafe_fields,
        }


def test_orchestrator_response_strips_executor_sensitive_field_names() -> None:
    db = _FakeSession()
    result = RetentionOrchestrator(
        db,  # type: ignore[arg-type]
        executors=(_LeakyExecutor(),),
        executor_order=(
            RetentionExecutorOrderEntry(
                order=10,
                executor_name=_LeakyExecutor.name,
                retention_class=_LeakyExecutor.retention_class,
                dependency_note="test executor",
            ),
        ),
        policy_resolver=lambda _db, _tenant_id: _Policy(),  # type: ignore[arg-type, return-value]
    ).run(
        RetentionRunRequest(
            mode=RETENTION_RUN_MODE_DRY_RUN,
            scope=RETENTION_SCOPE_TENANT,
            tenant_id=42,
            retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
        )
    )

    payload = RetentionRunResponse.from_run_result(result).model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["tenant_id"] == 42
    assert payload["counts"]["candidate_count"] == 3
    assert payload["counts"]["protected_count"] == 1
    assert payload["counts"]["skipped_count"] == 1
    assert payload["executor_results"][0]["executor_name"] == _LeakyExecutor.name
    assert (
        payload["executor_results"][0]["retention_class"]
        == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
    )
    assert payload["executor_results"][0]["reason_counts"] == {
        "expired_operational_record": 3,
        "protected_active_record": 1,
    }
    assert _unsafe_field_names_in(payload) == []
    for part in UNSAFE_SUMMARY_FIELD_PARTS:
        assert f"leaked-{part}" not in serialized
        assert f"decision-leak-{part}" not in serialized
    assert db.rollback_count == 1


@pytest.mark.parametrize("unsafe_part", UNSAFE_SUMMARY_FIELD_PARTS)
def test_audit_payload_rejects_sensitive_field_names(unsafe_part: str) -> None:
    with pytest.raises(UnsafeAuditPayloadError, match="unsafe audit payload field"):
        to_safe_audit_payload(
            {
                "run_id": "retention:42",
                f"raw_{unsafe_part}": "not allowed",
            }
        )


def test_audit_payload_strip_mode_removes_sensitive_fields_and_keeps_safe_summary() -> None:
    payload = {
        "run_id": "retention:42",
        "tenant_id": 42,
        "retention_class": RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        "safe_ids": ("runner-session:123",),
        "counts": {"candidate_count": 3, "protected_count": 1},
        "reason_counts": {"expired_operational_record": 3},
        "nested": {
            f"raw_{part}": f"leaked-{part}" for part in UNSAFE_SUMMARY_FIELD_PARTS
        },
    }

    safe_payload = to_safe_audit_payload(payload, strip_unsafe=True)
    serialized = json.dumps(safe_payload, sort_keys=True)

    assert safe_payload == {
        "run_id": "retention:42",
        "tenant_id": 42,
        "retention_class": RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        "safe_ids": ["runner-session:123"],
        "counts": {"candidate_count": 3, "protected_count": 1},
        "reason_counts": {"expired_operational_record": 3},
        "nested": {},
    }
    assert _unsafe_field_names_in(safe_payload) == []
    for part in UNSAFE_SUMMARY_FIELD_PARTS:
        assert f"leaked-{part}" not in serialized


def _unsafe_field_names_in(value: Any) -> list[str]:
    field_names: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_").replace(" ", "_")
            if any(part in normalized for part in UNSAFE_SUMMARY_FIELD_PARTS):
                field_names.append(str(key))
            field_names.extend(_unsafe_field_names_in(item))
    elif isinstance(value, list | tuple):
        for item in value:
            field_names.extend(_unsafe_field_names_in(item))
    return field_names
