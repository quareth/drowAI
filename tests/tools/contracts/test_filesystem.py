from __future__ import annotations

import pytest

from agent.tools.tool_registry import get_tool

from tests.tools.fixtures.parameter_fixtures import load_param_fixture

from .base_contract import BaseToolContract


FILESYSTEM_TOOLS = [
    "filesystem.read_file",
    "filesystem.write_file",
    "filesystem.append_file",
    "filesystem.delete_path",
    "filesystem.make_dir",
    "filesystem.list_dir",
    "filesystem.move_path",
    "filesystem.copy_path",
    "filesystem.stat_path",
    "filesystem.find_paths",
    "filesystem.search_text",
]


@pytest.mark.parametrize(
    "tool_id",
    [pytest.param(tool_id, marks=pytest.mark.tool(tool_id)) for tool_id in FILESYSTEM_TOOLS],
)
class TestFilesystemToolsContracts(BaseToolContract):
    """Contract tests for filesystem tools."""

    @pytest.fixture
    def tool_id(self, request):
        return request.param


class TestFilesystemPathSecurity:
    """Security tests specific to filesystem tools."""

    TOOLS_WITH_PATH = [
        ("filesystem.read_file", "path"),
        ("filesystem.write_file", "path"),
        ("filesystem.append_file", "path"),
        ("filesystem.delete_path", "path"),
        ("filesystem.make_dir", "path"),
        ("filesystem.list_dir", "path"),
        ("filesystem.stat_path", "path"),
        ("filesystem.find_paths", "path"),
        ("filesystem.search_text", "path"),
    ]

    TOOLS_WITH_SRC_DEST = [
        ("filesystem.move_path", "src", "dest"),
        ("filesystem.copy_path", "src", "dest"),
    ]

    @pytest.mark.parametrize("tool_id,field", TOOLS_WITH_PATH)
    def test_path_traversal_blocked(self, tool_id: str, field: str) -> None:
        """Verify path traversal attempts are blocked."""

        tool_cls = get_tool(tool_id)
        args_class = tool_cls.args_model
        base_args = load_param_fixture(tool_id)["test_cases"]["minimal"]["params"].copy()

        traversal_payloads = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "subdir/../../../etc/passwd",
        ]

        for payload in traversal_payloads:
            with pytest.raises(ValueError, match="escapes the workspace|Absolute paths"):
                tool = tool_cls()
                args = args_class(**{**base_args, field: payload})
                tool.build_command(args)

    @pytest.mark.parametrize("tool_id,field", TOOLS_WITH_PATH)
    def test_absolute_path_rejected(self, tool_id: str, field: str) -> None:
        """Verify absolute paths are rejected."""

        tool_cls = get_tool(tool_id)
        args_class = tool_cls.args_model
        base_args = load_param_fixture(tool_id)["test_cases"]["minimal"]["params"].copy()

        with pytest.raises(ValueError, match="Absolute paths"):
            args = args_class(**{**base_args, field: "/etc/passwd"})
            tool = tool_cls()
            tool.build_command(args)

    @pytest.mark.parametrize("tool_id,src_field,dest_field", TOOLS_WITH_SRC_DEST)
    def test_src_dest_traversal_blocked(
        self,
        tool_id: str,
        src_field: str,
        dest_field: str,
    ) -> None:
        """Verify src/dest traversal attempts are blocked."""

        tool_cls = get_tool(tool_id)
        args_class = tool_cls.args_model
        base_args = load_param_fixture(tool_id)["test_cases"]["minimal"]["params"].copy()

        with pytest.raises(ValueError, match="escapes the workspace|Absolute paths"):
            tool = tool_cls()
            args = args_class(**{**base_args, src_field: "../secret", dest_field: "copy.txt"})
            tool.build_command(args)

        with pytest.raises(ValueError, match="escapes the workspace|Absolute paths"):
            tool = tool_cls()
            args = args_class(**{**base_args, src_field: "file.txt", dest_field: "../../../etc/passwd"})
            tool.build_command(args)
