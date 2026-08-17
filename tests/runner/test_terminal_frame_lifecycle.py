"""Tests for data-first runner terminal frames and structured process exit."""

from drowai_runner.control_channel.terminal.frames import TerminalFrameLifecycle
from runtime_shared.runner_protocol import RUNNER_TERMINAL_FRAME_MAX_BYTES


class _OperationService:
    def __init__(self) -> None:
        self.responses = [
            {
                "status": "succeeded",
                "metadata": {
                    "output": "tail\n",
                    "eof": False,
                    "process_status": "running",
                    "exit_code": None,
                },
            },
            {
                "status": "succeeded",
                "metadata": {
                    "output": "",
                    "eof": True,
                    "process_status": "completed",
                    "exit_code": 0,
                },
            },
        ]

    def dispatch_operation(self, **_kwargs):
        return self.responses.pop(0)


def test_terminal_frames_deliver_tail_before_one_structured_exit_frame() -> None:
    service = _OperationService()
    lifecycle = TerminalFrameLifecycle(
        active_terminal_sessions={},
        terminal_frame_sequences={},
        terminal_frame_publishers={},
        operation_service_provider=lambda: service,
    )

    frames, should_drop = lifecycle.read_terminal_frames(session_id="session-1")

    assert frames == [
        {
            "session_id": "session-1",
            "sequence": 0,
            "stream": "stdout",
            "data": "tail\n",
        },
        {
            "session_id": "session-1",
            "sequence": 1,
            "stream": "stdout",
            "data": "",
            "eof": True,
            "process_status": "completed",
            "exit_code": 0,
        },
    ]
    assert should_drop is True


def test_terminal_frames_split_text_only_at_utf8_codepoint_boundaries() -> None:
    output = "a" * (RUNNER_TERMINAL_FRAME_MAX_BYTES - 1) + "€"
    service = _OperationService()
    service.responses = [
        {
            "status": "succeeded",
            "metadata": {
                "output": output,
                "eof": False,
                "process_status": "running",
                "exit_code": None,
            },
        },
        {
            "status": "succeeded",
            "metadata": {
                "output": "",
                "eof": True,
                "process_status": "completed",
                "exit_code": 0,
            },
        },
    ]
    lifecycle = TerminalFrameLifecycle(
        active_terminal_sessions={},
        terminal_frame_sequences={},
        terminal_frame_publishers={},
        operation_service_provider=lambda: service,
    )

    frames, should_drop = lifecycle.read_terminal_frames(session_id="session-utf8")

    data_frames = [frame for frame in frames if frame["data"]]
    assert "".join(str(frame["data"]) for frame in data_frames) == output
    assert all(
        len(str(frame["data"]).encode("utf-8")) <= RUNNER_TERMINAL_FRAME_MAX_BYTES
        for frame in data_frames
    )
    assert should_drop is True
