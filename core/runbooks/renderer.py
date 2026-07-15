"""Render loaded runbooks into prompt-ready guidance sections."""

from __future__ import annotations

from collections.abc import Sequence
import re

from core.runbooks.models import LoadedRunbook, RunbookStage


_STAGE_HEADING_RE = re.compile(
    r"^## Stage:\s*(?P<stage>[a-z_]+)\s*$",
    flags=re.MULTILINE,
)


def render_runbooks(
    runbooks: Sequence[LoadedRunbook],
    *,
    stage: RunbookStage | None = None,
) -> str:
    """Render loaded runbooks into one prompt section."""

    if not runbooks:
        return ""

    rendered_runbooks = []
    for runbook in runbooks:
        body = _body_for_stage(runbook, stage=stage)
        if not body:
            continue
        rendered_runbooks.append(
            f"Runbook: {runbook.name}\nDescription: {runbook.description}\n\n{body}"
        )
    if not rendered_runbooks:
        return ""
    return "Tool Runbooks:\n" + "\n\n".join(rendered_runbooks)


def _body_for_stage(runbook: LoadedRunbook, *, stage: RunbookStage | None) -> str:
    """Return the stage-specific body slice when a runbook has stage sections."""

    body = runbook.body.strip()
    if stage is None:
        return body

    matches = list(_STAGE_HEADING_RE.finditer(body))
    if not matches:
        return body

    for index, match in enumerate(matches):
        if match.group("stage") != stage.value:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[start:end].strip()
    return ""


__all__ = ["render_runbooks"]
