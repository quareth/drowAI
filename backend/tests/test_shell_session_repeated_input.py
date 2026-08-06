"""Repeated-input behavior tests for terminal-backed shell sessions.

The same public session must remain writable across multiple input operations,
return each output delta once, and close only after process completion.
"""

from __future__ import annotations

import pytest

from backend.tests.test_shell_session_service import (
    FakeTerminalManager,
    _identity,
    _service,
)
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellWriteRequest,
)
from runtime_shared.shell_session_framing import PTY_EXIT_CODE_MARKER


class _RepeatedInputTerminalManager(FakeTerminalManager):
    """Keep an interactive command open until its third input."""

    def __init__(self) -> None:
        super().__init__()
        self.input_count = 0

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        payload = data.encode() if isinstance(data, str) else data
        if b"__DROWAI_CMD_START_" in payload:
            return await super().send_input(session_id, payload)

        self.sent_inputs.append((session_id, payload))
        self.input_count += 1
        _start, end = self.session_markers[session_id]
        if self.input_count < 3:
            self.queues[session_id].append(
                f"input-{self.input_count}:".encode() + payload
            )
        else:
            self.queues[session_id].append(
                b"finished:"
                + payload
                + f"{end}={PTY_EXIT_CODE_MARKER}0\n".encode()
            )
        return True


@pytest.mark.asyncio
async def test_repeated_write_stdin_returns_distinct_deltas_until_completion() -> None:
    manager = _RepeatedInputTerminalManager()
    service = _service(manager)
    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(command="interactive", yield_time_ms=0),
    )

    assert started.process_status is ShellProcessStatus.RUNNING
    assert started.session_id is not None

    first = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(
            session_id=started.session_id,
            chars="one\n",
            yield_time_ms=0,
        ),
    )
    second = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(
            session_id=started.session_id,
            chars="two\n",
            yield_time_ms=0,
        ),
    )
    completed = await service.write_stdin(
        identity=_identity(),
        request=ShellWriteRequest(
            session_id=started.session_id,
            chars="done\n",
            yield_time_ms=0,
        ),
    )

    assert first.process_status is ShellProcessStatus.RUNNING
    assert first.stdout == "input-1:one"
    assert second.process_status is ShellProcessStatus.RUNNING
    assert second.stdout == "input-2:two"
    assert completed.process_status is ShellProcessStatus.COMPLETED
    assert completed.exit_code == 0
    assert completed.session_id is None
    assert completed.stdout == "finished:done"
    assert [payload for _session_id, payload in manager.sent_inputs[-3:]] == [
        b"one\n",
        b"two\n",
        b"done\n",
    ]
    assert manager.closed_sessions == ["terminal-1"]
