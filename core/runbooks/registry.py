"""Load discovered bundled runbooks from the builtin runbook root."""

from __future__ import annotations

import logging
from pathlib import Path

from core.runbooks.discovery import discover_runbook_paths
from core.runbooks.errors import RunbookError
from core.runbooks.loader import RunbookLoader
from core.runbooks.models import LoadedRunbook


DEFAULT_BUILTIN_RUNBOOK_ROOT = Path(__file__).resolve().parent / "builtin"
DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT = DEFAULT_BUILTIN_RUNBOOK_ROOT / "tool_runbooks"
logger = logging.getLogger(__name__)


class RunbookRegistry:
    """Loads builtin tool runbooks discovered from RUNBOOK.md assets."""

    def __init__(
        self,
        *,
        builtin_root: Path | str = DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT,
        loader: RunbookLoader | None = None,
    ) -> None:
        self._builtin_root = Path(builtin_root)
        self._loader = loader or RunbookLoader()

    def load_builtin_tool_runbooks(self) -> tuple[LoadedRunbook, ...]:
        """Load discovered builtin tool runbooks in deterministic path order."""

        runbooks: list[LoadedRunbook] = []
        seen_ids: set[str] = set()
        for path in discover_runbook_paths(self._builtin_root):
            try:
                runbook = self._loader.load(path)
            except RunbookError as exc:
                logger.warning("Skipping invalid runbook %s: %s", path, exc)
                continue

            if runbook.id in seen_ids:
                logger.warning(
                    "Skipping duplicate runbook id %r from %s",
                    runbook.id,
                    path,
                )
                continue

            seen_ids.add(runbook.id)
            runbooks.append(runbook)

        return tuple(runbooks)


__all__ = [
    "DEFAULT_BUILTIN_RUNBOOK_ROOT",
    "DEFAULT_BUILTIN_TOOL_RUNBOOK_ROOT",
    "RunbookRegistry",
]
