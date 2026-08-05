"""Tests for the backend-free terminal manager resolver port."""

import pytest

from runtime_shared import terminal_manager_port
from runtime_shared.terminal_contracts import TerminalReadResult


class _TypedTerminalManager:
    async def prepare_agent_session(self, *args: object, **kwargs: object) -> None:
        return None

    async def send_input(self, *args: object, **kwargs: object) -> bool:
        return True

    async def read_output(self, *args: object, **kwargs: object) -> bytes:
        return b"legacy-bytes"

    async def read_output_result(
        self,
        session_id: str,
        size: int = 4096,
        *,
        timeout: float | None = None,
    ) -> TerminalReadResult:
        assert session_id == "term-1"
        assert size == 64
        assert timeout == 0.1
        return TerminalReadResult(ok=True, data=b"typed-bytes")

    async def close_session(self, *args: object, **kwargs: object) -> bool:
        return True


def test_get_terminal_session_manager_raises_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal_manager_port, "_terminal_manager_resolver", None)

    with pytest.raises(RuntimeError, match="resolver is not configured"):
        terminal_manager_port.get_terminal_session_manager()


@pytest.mark.asyncio
async def test_resolver_exposes_typed_reads_without_changing_legacy_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _TypedTerminalManager()
    monkeypatch.setattr(terminal_manager_port, "_terminal_manager_resolver", lambda: manager)

    resolved = terminal_manager_port.get_terminal_session_manager()

    assert await resolved.read_output("term-1", 64, timeout=0.1) == b"legacy-bytes"
    typed = await resolved.read_output_result("term-1", 64, timeout=0.1)
    assert typed == TerminalReadResult(ok=True, data=b"typed-bytes")
