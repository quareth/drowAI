"""Repeated exact-input behavior for explicit interactive shell execution."""

from __future__ import annotations

import pytest

from backend.tests.test_shell_session_service import (
    _CommandTerminalManager,
    _drain_terminal,
    _identity,
    _service,
)
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellWriteRequest,
)
from runtime_shared.terminal_contracts import TerminalReadResult


class _RepeatedInputManager(_CommandTerminalManager):
    def __init__(self) -> None:
        super().__init__()
        self.input_count = 0

    async def send_input(self, session_id: str, data: bytes | str) -> bool:
        payload = data.encode() if isinstance(data, str) else data
        self.sent_inputs.append((session_id, payload))
        self.input_count += 1
        if self.input_count < 3:
            self.reads.setdefault(session_id, []).extend(
                [
                    TerminalReadResult(
                        ok=True,
                        data=f"input-{self.input_count}:".encode() + payload,
                    ),
                ]
            )
        else:
            self.reads.setdefault(session_id, []).extend(
                [
                    TerminalReadResult(ok=True, data=b"finished:" + payload),
                    TerminalReadResult(
                        ok=True,
                        eof=True,
                        process_status="completed",
                        exit_code=0,
                    ),
                ]
            )
        return True


@pytest.mark.asyncio
async def test_repeated_input_returns_distinct_deltas_until_real_exit() -> None:
    manager = _RepeatedInputManager()
    service = _service(manager)
    started = await service.execute(
        identity=_identity(),
        request=ShellExecRequest(
            command="interactive-command",
            interactive=True,
            yield_time_ms=0,
        ),
        capability=ShellCapability.UTILITY,
    )

    updates = []
    for chars in ("one\n", "two\n", "done\n"):
        updates.append(
            await service.write_stdin(
                identity=_identity(),
                request=ShellWriteRequest(
                    session_id=str(started.session_id),
                    chars=chars,
                    yield_time_ms=0,
                ),
            )
        )
    terminal = await _drain_terminal(service, updates[-1])

    assert [update.process_status for update in updates] == [
        ShellProcessStatus.RUNNING,
        ShellProcessStatus.RUNNING,
        ShellProcessStatus.RUNNING,
    ]
    assert [update.stdout for update in updates] == [
        "input-1:one\n",
        "input-2:two\n",
        "finished:done\n",
    ]
    assert terminal.process_status is ShellProcessStatus.COMPLETED
