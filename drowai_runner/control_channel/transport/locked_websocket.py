"""Thread-safe websocket send wrapper shared by the main loop and terminal publishers.

Serializes concurrent sends behind a lock while passing receives straight
through; no protocol knowledge, no I/O beyond the wrapped websocket.
"""

from __future__ import annotations

import threading


class _LockedWebSocket:
    """Serialize websocket sends shared by the main loop and terminal publishers."""

    def __init__(self, websocket: object) -> None:
        self._websocket = websocket
        self._send_lock = threading.Lock()

    def send(self, payload: str) -> None:
        with self._send_lock:
            self._websocket.send(payload)

    def recv(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return self._websocket.recv(*args, **kwargs)
