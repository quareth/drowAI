"""Tests for backend-free terminal session contracts."""

from runtime_shared.terminal_contracts import (
    AGENT_PROMPT_ENV,
    AGENT_PROMPT_MARKER,
    TerminalReadResult,
    TerminalSessionIdentity,
    TerminalSessionSnapshot,
    build_agent_session_id,
    build_named_agent_session_id,
)


def test_terminal_contracts_preserve_existing_values_and_dtos() -> None:
    assert AGENT_PROMPT_MARKER == "__DROWAI_PROMPT__> "
    assert AGENT_PROMPT_ENV == "__DROWAI_PROMPT__>"
    assert build_agent_session_id(123) == "agent_task_123"
    assert build_named_agent_session_id(456, "my-session.name") == "agent_task_456_my_session_name"

    identity = TerminalSessionIdentity(
        task_id=123,
        session_name="agent",
        session_id="agent_task_123",
    )
    snapshot = TerminalSessionSnapshot(
        task_id=123,
        session_id="agent_task_123",
        session_name="agent",
    )

    assert identity.session_type == "agent"
    assert snapshot.runtime_job_id is None
    assert snapshot.container_id is None


def test_terminal_read_result_distinguishes_idle_success_from_failure() -> None:
    idle = TerminalReadResult(ok=True)
    failure = TerminalReadResult(ok=False, error_code="runtime_transport_failed")

    assert idle.ok is True
    assert idle.data == b""
    assert idle.error_code is None
    assert failure.ok is False
    assert failure.data == b""
    assert failure.error_code == "runtime_transport_failed"
