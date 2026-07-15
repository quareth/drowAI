"""Tests for resolving active tool runbooks by selected tool ids and stage."""

from __future__ import annotations

from core.runbooks.models import LoadedRunbook, RunbookStage, RunbookType
from core.runbooks.resolver import resolve_for_categories, resolve_for_tools


def test_empty_selected_tools_returns_empty_result():
    runbooks = (
        _runbook(
            "filesystem_artifact_reading",
            trigger_tool_ids=("filesystem.read_file",),
        ),
    )

    assert (
        resolve_for_tools(
            runbooks=runbooks,
            selected_tools=[],
            stage=RunbookStage.TOOL_PARAMETERS,
        )
        == []
    )


def test_tool_id_matching_is_exact():
    runbook = _runbook(
        "filesystem_artifact_reading",
        trigger_tool_ids=("filesystem.read_file",),
    )

    assert resolve_for_tools(
        runbooks=(runbook,),
        selected_tools=["filesystem.read_file.extra"],
        stage=RunbookStage.TOOL_PARAMETERS,
    ) == []
    assert resolve_for_tools(
        runbooks=(runbook,),
        selected_tools=["filesystem.read_file"],
        stage=RunbookStage.TOOL_PARAMETERS,
    ) == [runbook]


def test_runbook_is_returned_only_when_requested_stage_is_enabled():
    runbook = _runbook(
        "filesystem_artifact_reading",
        trigger_tool_ids=("filesystem.read_file",),
        stages=(RunbookStage.PLANNER,),
    )

    assert resolve_for_tools(
        runbooks=(runbook,),
        selected_tools=["filesystem.read_file"],
        stage=RunbookStage.TOOL_PARAMETERS,
    ) == []
    assert resolve_for_tools(
        runbooks=(runbook,),
        selected_tools=["filesystem.read_file"],
        stage=RunbookStage.PLANNER,
    ) == [runbook]


def test_matching_runbooks_preserve_input_order():
    first = _runbook(
        "filesystem_artifact_reading",
        trigger_tool_ids=("filesystem.read_file",),
    )
    second = _runbook(
        "filesystem_artifact_search",
        trigger_tool_ids=("filesystem.search_text",),
    )
    unrelated = _runbook(
        "terminal_guidance",
        trigger_tool_ids=("terminal.execute",),
    )

    assert resolve_for_tools(
        runbooks=(first, unrelated, second),
        selected_tools=["filesystem.search_text", "filesystem.read_file"],
        stage=RunbookStage.TOOL_PARAMETERS,
    ) == [first, second]


def test_resolver_matches_filesystem_search_text():
    runbook = _runbook(
        "filesystem_artifact_search",
        trigger_tool_ids=("filesystem.search_text",),
    )

    assert resolve_for_tools(
        runbooks=(runbook,),
        selected_tools=["filesystem.search_text"],
        stage=RunbookStage.TOOL_PARAMETERS,
    ) == [runbook]


def test_category_id_matching_is_exact():
    runbook = _runbook(
        "web_discovery",
        trigger_tool_ids=(),
        trigger_category_ids=("web_applications",),
        stages=(RunbookStage.TOOL_SELECTION,),
    )

    assert resolve_for_categories(
        runbooks=(runbook,),
        selected_categories=["information_gathering"],
        stage=RunbookStage.TOOL_SELECTION,
    ) == []
    assert resolve_for_categories(
        runbooks=(runbook,),
        selected_categories=["web_applications"],
        stage=RunbookStage.TOOL_SELECTION,
    ) == [runbook]


def _runbook(
    runbook_id: str,
    *,
    trigger_tool_ids: tuple[str, ...],
    trigger_category_ids: tuple[str, ...] = (),
    stages: tuple[RunbookStage, ...] = (RunbookStage.TOOL_PARAMETERS,),
) -> LoadedRunbook:
    return LoadedRunbook(
        id=runbook_id,
        name=runbook_id.replace("_", " ").title(),
        type=RunbookType.TOOL,
        version=1,
        description=f"{runbook_id} guidance.",
        trigger_tool_ids=trigger_tool_ids,
        trigger_category_ids=trigger_category_ids,
        stages=stages,
        body=f"{runbook_id} instructions.",
    )
