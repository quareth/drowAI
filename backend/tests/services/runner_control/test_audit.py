"""Tests for runner-control audit event redaction and envelope shaping."""

from __future__ import annotations

from backend.services.runner_control.audit import RunnerControlAuditService, redact_audit_metadata


def test_redact_audit_metadata_masks_secret_keys_and_values() -> None:
    metadata = {
        "runner_secret": "rsec_very_secret_value",
        "install_token": "rit_sensitive_token",
        "upload_url": "https://object.example/upload?X-Amz-Signature=abc123",
        "nested": {
            "authorization": "Bearer abc123",
            "download_url": "https://object.example/download?token=topsecret",
            "safe_key": "safe-value",
        },
        "values": ["plain", "rsec_list_secret"],
    }

    redacted = redact_audit_metadata(metadata)

    assert redacted["runner_secret"] == "<REDACTED>"
    assert redacted["install_token"] == "<REDACTED>"
    assert redacted["upload_url"] == "<REDACTED>"
    assert redacted["nested"]["authorization"] == "<REDACTED>"
    assert redacted["nested"]["download_url"] == "<REDACTED>"
    assert redacted["nested"]["safe_key"] == "safe-value"
    assert redacted["values"][1] == "<REDACTED>"


def test_audit_service_emits_required_identity_fields() -> None:
    events: list[dict[str, object]] = []
    service = RunnerControlAuditService(emitter=events.append)

    service.emit(
        event_type="runner.message.accepted",
        tenant_id=7,
        runner_id="runner-123",
        task_id=99,
        runtime_job_id="job-55",
        correlation_id="corr-1",
        metadata={"message_type": "runner.heartbeat"},
    )

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "runner.message.accepted"
    assert event["tenant_id"] == 7
    assert event["runner_id"] == "runner-123"
    assert event["task_id"] == 99
    assert event["runtime_job_id"] == "job-55"
    assert event["correlation_id"] == "corr-1"
    assert event["actor_user_id"] is None
    assert event["resource_type"] == "runtime_job"
    assert event["resource_id"] == "job-55"
    assert event["action"] == "accept"
    assert event["result"] == "success"
    assert event["reason_code"] == "NONE"


def test_audit_service_infers_actor_and_reason_code_from_metadata() -> None:
    events: list[dict[str, object]] = []
    service = RunnerControlAuditService(emitter=events.append)

    service.emit(
        event_type="runner.protocol_violation",
        tenant_id=7,
        runner_id="runner-123",
        metadata={
            "created_by_user_id": 42,
            "error_code": "runner auth invalid",
        },
    )

    event = events[0]
    assert event["actor_user_id"] == 42
    assert event["resource_type"] == "runner"
    assert event["resource_id"] == "runner-123"
    assert event["action"] == "validate"
    assert event["result"] == "failure"
    assert event["reason_code"] == "RUNNER_AUTH_INVALID"
