"""Optional diagnostic logging helpers for LangGraph builder graph builds and wrappers.

This module centralizes the import-time try/except for the backend
diagnostic logger so both the deep-reasoning and simple-tool builders
share the same fallback behavior. It also exposes a small wrapper-callback
factory so DR and simple-tool can plug the same diagnostic signal into
the Tier 2 ``on_wrap_log`` hook on ``common_edges.wrap_with_context*``.

Boundary
--------
Backend diagnostic imports must stay out of ``common_edges.py``. They
live here, behind a try/except fallback, so that:

- Tests and tooling that import builder modules without the backend
  service tree available still load successfully (helpers degrade to
  no-ops).
- Adding diagnostics to a new builder is a one-import change, not a
  copy/paste of import-time fallback boilerplate.

Helpers exposed
---------------
- :func:`get_builder_diagnostic_logger` — return the optional backend
  diagnostic logger (or ``None`` when unavailable).
- :func:`log_builder_graph_build` — proxy to the backend
  ``log_graph_build`` helper, no-op when diagnostics are unavailable.
- :func:`make_wrapper_log_callback` — build a wrapper callback that
  matches :data:`common_edges.WrapperLogCallback` and proxies to the
  backend ``log_wrapper_context`` helper.
"""

from __future__ import annotations

from typing import Any, Optional

from .common_edges import WrapperLogCallback


# Resolve backend diagnostic helpers once at import time. If the backend
# service tree is unavailable (tests, tooling) the helpers degrade to
# no-ops without raising at import time.
try:  # pragma: no cover - import-time fallback
    from backend.services.langgraph_chat.diagnostic_logger import (
        get_diagnostic_logger as _backend_get_diagnostic_logger,
        log_graph_build as _backend_log_graph_build,
        log_wrapper_context as _backend_log_wrapper_context,
    )

    _DIAGNOSTICS_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback path
    _backend_get_diagnostic_logger = None  # type: ignore[assignment]
    _backend_log_graph_build = None  # type: ignore[assignment]
    _backend_log_wrapper_context = None  # type: ignore[assignment]
    _DIAGNOSTICS_AVAILABLE = False


def get_builder_diagnostic_logger() -> Optional[Any]:
    """Return the backend diagnostic logger when available, otherwise ``None``.

    Builders that want to emit free-form diagnostic ``info``/``debug``
    lines (in addition to the structured ``log_*`` helpers) can call
    this and check truthiness before logging:

    .. code-block:: python

        diag = get_builder_diagnostic_logger()
        if diag:
            diag.info("BUILDER | starting compile | ...")
    """
    if not _DIAGNOSTICS_AVAILABLE or _backend_get_diagnostic_logger is None:
        return None
    return _backend_get_diagnostic_logger()


def log_builder_graph_build(
    graph_name: str,
    checkpointer_name: str,
    node_count: int,
) -> None:
    """Log a builder graph build through the backend diagnostic helper.

    Mirrors the existing ``backend.services.langgraph_chat.diagnostic_logger.log_graph_build``
    payload shape so existing dashboards/log consumers do not break:
    ``"BUILDER | Building <graph_name> | checkpointer=<checkpointer_name>, nodes=<node_count>"``.
    Degrades to a no-op when backend diagnostics are unavailable.
    """
    if not _DIAGNOSTICS_AVAILABLE or _backend_log_graph_build is None:
        return
    _backend_log_graph_build(graph_name, checkpointer_name, node_count)


def make_wrapper_log_callback() -> WrapperLogCallback:
    """Return a ``WrapperLogCallback`` that bridges to the backend diagnostic helper.

    Usable directly as the ``on_wrap_log`` keyword argument on
    ``common_edges.wrap_with_context`` and
    ``common_edges.wrap_with_context_async``. The returned callable
    matches :data:`common_edges.WrapperLogCallback`
    (``(node_name: str, writer_available: bool, config_available: bool)``)
    and is a no-op when backend diagnostics are unavailable.
    """

    def _callback(
        node_name: str,
        writer_available: bool,
        config_available: bool,
    ) -> None:
        if not _DIAGNOSTICS_AVAILABLE or _backend_log_wrapper_context is None:
            return
        _backend_log_wrapper_context(node_name, writer_available, config_available)

    return _callback


__all__ = [
    "get_builder_diagnostic_logger",
    "log_builder_graph_build",
    "make_wrapper_log_callback",
]
