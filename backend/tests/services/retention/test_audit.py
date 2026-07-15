"""Tests for no-secret retention audit payload helpers."""

from __future__ import annotations

import pytest

from backend.services.retention.audit import (
    UNSAFE_AUDIT_KEY_PARTS,
    UnsafeAuditPayloadError,
    normalize_audit_reason_code,
    to_safe_audit_payload,
)


@pytest.mark.parametrize("unsafe_part", sorted(UNSAFE_AUDIT_KEY_PARTS))
def test_safe_audit_payload_rejects_unsafe_keys_recursively(
    unsafe_part: str,
) -> None:
    payload = {
        "run_id": "retention:1",
        "nested": [{"reason_code": "ok", f"raw_{unsafe_part}": "not allowed"}],
    }

    with pytest.raises(UnsafeAuditPayloadError, match="unsafe audit payload field"):
        to_safe_audit_payload(payload)


def test_safe_audit_payload_normalizes_key_variants() -> None:
    with pytest.raises(UnsafeAuditPayloadError, match="unsafe audit payload field"):
        to_safe_audit_payload({"object-key": "s3/path"})

    with pytest.raises(UnsafeAuditPayloadError, match="unsafe audit payload field"):
        to_safe_audit_payload({"Authorization Header": "Bearer value"})


def test_safe_audit_payload_can_strip_unsafe_keys() -> None:
    payload = {
        "run_id": "retention:1",
        "safe_counts": {"deleted": 2},
        "token": "not allowed",
        "nested": {
            "reason_code": "terminal_task_expired",
            "raw_content": "not allowed",
        },
    }

    assert to_safe_audit_payload(payload, strip_unsafe=True) == {
        "run_id": "retention:1",
        "safe_counts": {"deleted": 2},
        "nested": {"reason_code": "terminal_task_expired"},
    }


def test_audit_reason_codes_use_contract_normalization() -> None:
    assert (
        normalize_audit_reason_code("Operational_Log_Retention_Expired")
        == "operational_log_retention_expired"
    )
    with pytest.raises(ValueError, match="invalid retention reason code"):
        normalize_audit_reason_code("contains spaces")
