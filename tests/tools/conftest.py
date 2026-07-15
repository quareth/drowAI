from __future__ import annotations

import os
from typing import List

import pytest

from tests.tools.registry.manifest_manager import ManifestManager


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--tool-ids",
        action="store",
        default="",
        help="Comma-separated tool IDs to test",
    )
    parser.addoption(
        "--pending-only",
        action="store_true",
        help="Only test tools with pending status",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "tool(id): mark test for specific tool")


def pytest_collection_modifyitems(config: pytest.Config, items: List[pytest.Item]) -> None:
    tool_ids = config.getoption("tool_ids", default="")
    pending_only = config.getoption("pending_only", default=False)

    selected_tool_ids: List[str] = []
    if tool_ids:
        selected_tool_ids = [tool.strip() for tool in tool_ids.split(",") if tool.strip()]
    elif pending_only:
        manifest = ManifestManager()
        selected_tool_ids = manifest.get_untested_tools()

    if not selected_tool_ids:
        return

    selected_set = set(selected_tool_ids)
    kept: List[pytest.Item] = []
    deselected: List[pytest.Item] = []
    for item in items:
        marker = item.get_closest_marker("tool")
        if marker and marker.args and marker.args[0] in selected_set:
            kept.append(item)
        else:
            deselected.append(item)

    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = kept


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _ = exitstatus
    # Default to no manifest writes during routine pytest runs to avoid
    # unintended git diffs from timestamp-only updates.
    if os.getenv("TOOL_TESTS_PERSIST_MANIFEST", "false").strip().lower() != "true":
        return

    manifest = ManifestManager()
    has_updates = False
    for item in session.items:
        marker = item.get_closest_marker("tool")
        if not marker or not marker.args:
            continue
        tool_id = marker.args[0]
        rep_call = getattr(item, "rep_call", None)
        if rep_call is None or rep_call.outcome == "skipped":
            continue
        manifest.update_tool_result(tool_id, item.name, rep_call.passed)
        has_updates = True
    if has_updates:
        manifest.save_manifest()
