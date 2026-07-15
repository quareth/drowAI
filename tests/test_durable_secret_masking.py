"""Tests for the shared durable-only secret masking helper."""

from __future__ import annotations

import json

from runtime_shared.durable_secret_masking import mask_durable_secrets


def test_mask_durable_secrets_recursively_masks_reusable_values() -> None:
    raw_token = "durable-secret-token"
    raw_password = "correct-horse-battery"
    payload = {
        "command": f"curl -H 'Authorization: Bearer {raw_token}' http://192.0.2.10:8080/login",
        "metadata": {
            "ip": "192.0.2.10",
            "port": 8080,
            "protocol": "http",
            "password": raw_password,
            "path": "/workspace/artifacts/raw.txt",
        },
        "rows": [
            {"field": "http.authorization", "proof_excerpt": f"Bearer {raw_token}"},
        ],
    }

    masked = mask_durable_secrets(payload, source="unit")
    serialized = json.dumps(masked, sort_keys=True)

    assert raw_token not in serialized
    assert raw_password not in serialized
    assert "<DURABLE_SECRET_MASK:token>" in serialized
    assert "<DURABLE_SECRET_MASK:secret>" in serialized
    assert "192.0.2.10" in serialized
    assert "8080" in serialized
    assert "/workspace/artifacts/raw.txt" in serialized


def test_mask_durable_secrets_preserves_safe_markers() -> None:
    payload = {"api_key": "<KEY_SET>", "nested": {"token": "<NO_KEY>"}}

    assert mask_durable_secrets(payload, source="unit") == payload


def test_mask_durable_secrets_masks_json_and_log_style_sensitive_fields() -> None:
    raw_secret = "PocSecret-DurableMasking-Sentinel-9f4c2a"
    payloads = [
        f'{{"ftp.request.command_parameter": "{raw_secret}"}}',
        f"ftp.request.command_parameter={raw_secret}",
        {"ftp.request.command_parameter": raw_secret},
    ]

    masked = mask_durable_secrets(payloads, source="unit")
    serialized = json.dumps(masked)

    assert raw_secret not in serialized
    assert serialized.count("<DURABLE_SECRET_MASK:secret>") == 3


def test_mask_durable_secrets_masks_bare_tshark_secret_exposure_proof() -> None:
    raw_secret = "PocSecret-DurableMasking-Sentinel-9f4c2a"
    payload = {
        "metadata": {
            "secret_exposure": [
                {
                    "field": "ftp.request.command_parameter",
                    "kind": "protocol_auth_argument",
                    "proof_mode": "proof_excerpt",
                    "proof_excerpt": raw_secret,
                }
            ]
        }
    }

    masked = mask_durable_secrets(payload, source="unit")
    serialized = json.dumps(masked, sort_keys=True)

    assert raw_secret not in serialized
    assert "<DURABLE_SECRET_MASK:secret>" in serialized


def test_mask_durable_secrets_preserves_runner_terminal_session_ids() -> None:
    payload = {
        "source": "runner_event",
        "message_type": "terminal.result",
        "operation_id": "open_terminal_session:abc",
        "status": "succeeded",
        "terminal_operation": "open",
        "session_id": "terminal-session-abc123",
        "result": {
            "runtime_job_id": "2df89225-8205-48da-9f48-63138e29d37e",
            "session_id": "terminal-session-abc123",
            "password": "should-not-persist",
        },
    }

    masked = mask_durable_secrets(payload, source="runtime_job_runner_event_result")

    assert masked["session_id"] == "terminal-session-abc123"
    assert masked["result"]["session_id"] == "terminal-session-abc123"
    assert masked["result"]["password"] == "<DURABLE_SECRET_MASK:secret>"


def test_mask_durable_secrets_preserves_runner_terminal_command_params() -> None:
    for operation_name in ("close_terminal_session", "read_terminal_output"):
        payload = {
            "operation_name": operation_name,
            "params": {
                "session_id": "terminal-session-close-123",
                "token": "should-not-persist",
            },
        }

        masked = mask_durable_secrets(payload, source="runtime_job_payload")

        assert masked["params"]["session_id"] == "terminal-session-close-123"
        assert masked["params"]["token"] == "<DURABLE_SECRET_MASK:secret>"


def test_mask_durable_secrets_still_masks_non_terminal_session_ids() -> None:
    payload = {
        "message_type": "runtime.status",
        "session_id": "browser-session-abc123",
        "result": {"session_id": "nested-browser-session-abc123"},
    }

    masked = mask_durable_secrets(payload, source="runtime_job_result")

    assert masked["session_id"] == "<DURABLE_SECRET_MASK:secret>"
    assert masked["result"]["session_id"] == "<DURABLE_SECRET_MASK:secret>"
