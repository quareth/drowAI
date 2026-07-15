from __future__ import annotations

import argparse
import os
from typing import List

import pytest

from tests.tools.fixtures.fixture_generator import FixtureGenerator
from tests.tools.registry.manifest_manager import ManifestManager
from tests.tools.registry.tool_scanner import ToolScanner
from agent.tools.tool_registry import get_tool


def _run_pytest(tool_ids: List[str]) -> int:
    args = ["tests/tools/", "-v"]
    if tool_ids:
        args.append(f"--tool-ids={','.join(tool_ids)}")
    previous = os.environ.get("TOOL_TESTS_PERSIST_MANIFEST")
    os.environ["TOOL_TESTS_PERSIST_MANIFEST"] = "true"
    try:
        return pytest.main(args)
    finally:
        if previous is None:
            os.environ.pop("TOOL_TESTS_PERSIST_MANIFEST", None)
        else:
            os.environ["TOOL_TESTS_PERSIST_MANIFEST"] = previous


def command_discover() -> int:
    scanner = ToolScanner()
    new_tools = scanner.find_new_tools()
    print(f"Found {len(new_tools)} new tools:")
    for tool_id in new_tools:
        print(f"  - {tool_id}")
    return 0


def command_sync() -> int:
    scanner = ToolScanner()
    new_tools = scanner.sync_manifest()
    print(f"Manifest synchronized. Added {len(new_tools)} tools.")
    return 0


def command_generate_fixture(tool_id: str) -> int:
    tool_cls = get_tool(tool_id)
    generator = FixtureGenerator()
    generator.generate_all(tool_id, tool_cls)
    print(f"Fixtures generated for {tool_id}")
    return 0


def command_test(category: str | None, pending_only: bool, tool_id: str | None) -> int:
    manifest = ManifestManager()
    if tool_id:
        tool_ids = [tool_id]
    elif pending_only:
        tool_ids = manifest.get_untested_tools()
    elif category:
        tool_ids = manifest.get_tools_by_category(category)
    else:
        tool_ids = manifest.get_all_tools()
    return _run_pytest(tool_ids)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tool testing CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("discover", help="Discover new tools not in manifest")
    subparsers.add_parser("sync", help="Sync manifest with registry")

    generate_parser = subparsers.add_parser("generate-fixture", help="Generate fixtures for a tool")
    generate_parser.add_argument("tool_id", help="Tool ID to generate fixtures for")

    test_parser = subparsers.add_parser("test", help="Run tool tests")
    test_parser.add_argument("--category", help="Test specific category only")
    test_parser.add_argument("--pending-only", action="store_true", help="Only test untested tools")
    test_parser.add_argument("--tool", dest="tool_id", help="Test specific tool by ID")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "discover":
        return command_discover()
    if args.command == "sync":
        return command_sync()
    if args.command == "generate-fixture":
        return command_generate_fixture(args.tool_id)
    if args.command == "test":
        return command_test(args.category, args.pending_only, args.tool_id)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
