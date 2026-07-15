"""Terminal service package.

Responsibilities:
- Provide shared terminal session contracts, models, registry, and manager code.
- Keep terminal-specific orchestration grouped away from unrelated service modules.

Boundary:
- Public compatibility imports continue to live in `backend.services.terminal_session_manager`.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "contracts",
    "manager",
    "models",
    "registry",
    "ws_handler",
    "handle_terminal_ws",
]


def __getattr__(name: str) -> Any:
    """Lazy-load WebSocket helpers without importing manager on package import."""
    if name == "handle_terminal_ws":
        from .ws_handler import handle_terminal_ws

        return handle_terminal_ws
    raise AttributeError(name)
