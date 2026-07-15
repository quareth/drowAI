"""Load bundled runbook markdown files into validated runbook contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from core.runbooks.errors import (
    RunbookLoadError,
    RunbookParseError,
    RunbookValidationError,
)
from core.runbooks.models import LoadedRunbook


class RunbookLoader:
    """Loads one RUNBOOK.md file into a typed runbook object."""

    def load(self, path: Path | str) -> LoadedRunbook:
        runbook_path = Path(path)
        try:
            raw_text = runbook_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunbookLoadError(f"Unable to read runbook {runbook_path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise RunbookLoadError(f"Runbook {runbook_path} must be UTF-8 encoded") from exc

        frontmatter, body = _parse_markdown_frontmatter(raw_text, source=runbook_path)
        try:
            return LoadedRunbook.model_validate({**frontmatter, "body": body})
        except ValidationError as exc:
            raise RunbookValidationError(
                f"Runbook {runbook_path} metadata or body is invalid: {exc}"
            ) from exc


def load_runbook(path: Path | str) -> LoadedRunbook:
    """Load a single runbook markdown file with the default loader."""

    return RunbookLoader().load(path)


def _parse_markdown_frontmatter(
    raw_text: str, *, source: Path
) -> tuple[dict[str, Any], str]:
    lines = raw_text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise RunbookParseError(f"Runbook {source} must start with YAML frontmatter")

    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        raise RunbookParseError(f"Runbook {source} has malformed YAML frontmatter")

    frontmatter_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    try:
        parsed = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise RunbookParseError(f"Runbook {source} frontmatter is invalid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise RunbookParseError(f"Runbook {source} frontmatter must be a mapping")

    return parsed, body


__all__ = ["RunbookLoader", "load_runbook"]
