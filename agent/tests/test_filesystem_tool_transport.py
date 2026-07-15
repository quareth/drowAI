"""Tests for filesystem tool transport parameter support.

This test suite verifies.4 requirements:
- Schema validation: All tools accept transport parameter
- Metadata registration: All tools registered with transport support
- Backward compatibility: Tools work without transport parameter
- Documentation: All schemas have transport field documentation
- Validation: Invalid transport values are rejected"""

import pytest
from pydantic import ValidationError

from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.filesystem.contracts import (
    FsReadArgs,
    FsWriteArgs,
    FsAppendArgs,
    FsDeleteArgs,
    FsMakeDirArgs,
    FsListArgs,
    FsMoveArgs,
    FsCopyArgs,
    FsStatArgs,
    FsFindArgs,
    FsSearchTextArgs,
)

# All filesystem tools that should support transport (canonical namespace)
FS_TOOLS = [
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

# Valid transport values (filesystem tools only support file-comm and pty, not direct)
TRANSPORTS = ["file-comm", "pty"]

# Schema mapping for parameterized tests
SCHEMA_MAP = {
    "filesystem.read_file": FsReadArgs,
    "filesystem.write_file": FsWriteArgs,
    "filesystem.append_file": FsAppendArgs,
    "filesystem.delete_path": FsDeleteArgs,
    "filesystem.make_dir": FsMakeDirArgs,
    "filesystem.list_dir": FsListArgs,
    "filesystem.move_path": FsMoveArgs,
    "filesystem.copy_path": FsCopyArgs,
    "filesystem.stat_path": FsStatArgs,
    "filesystem.find_paths": FsFindArgs,
    "filesystem.search_text": FsSearchTextArgs,
}


class TestFilesystemToolTransportSchema:
    """Test transport parameter in filesystem tool schemas (Phase 3.2.4.2)."""

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_read_accepts_transport(self, transport):
        """Test FsReadArgs accepts valid transport values."""
        args = FsReadArgs(path="test.txt", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_write_accepts_transport(self, transport):
        """Test FsWriteArgs accepts valid transport values."""
        args = FsWriteArgs(path="test.txt", content="data", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_append_accepts_transport(self, transport):
        """Test FsAppendArgs accepts valid transport values."""
        args = FsAppendArgs(path="test.txt", content="data", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_delete_accepts_transport(self, transport):
        """Test FsDeleteArgs accepts valid transport values."""
        args = FsDeleteArgs(path="test.txt", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_make_dir_accepts_transport(self, transport):
        """Test FsMakeDirArgs accepts valid transport values."""
        args = FsMakeDirArgs(path="test_dir", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_list_accepts_transport(self, transport):
        """Test FsListArgs accepts valid transport values."""
        args = FsListArgs(path="test_dir", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_move_accepts_transport(self, transport):
        """Test FsMoveArgs accepts valid transport values."""
        args = FsMoveArgs(src="old.txt", dest="new.txt", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_copy_accepts_transport(self, transport):
        """Test FsCopyArgs accepts valid transport values."""
        args = FsCopyArgs(src="old.txt", dest="new.txt", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_stat_accepts_transport(self, transport):
        """Test FsStatArgs accepts valid transport values."""
        args = FsStatArgs(path="test.txt", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_find_accepts_transport(self, transport):
        """Test FsFindArgs accepts valid transport values."""
        args = FsFindArgs(filename_glob="*.txt", transport=transport)
        assert args.transport == transport

    @pytest.mark.parametrize("transport", [None, "file-comm", "pty"])
    def test_fs_search_text_accepts_transport(self, transport):
        """Test FsSearchTextArgs accepts valid transport values."""
        args = FsSearchTextArgs(query="test", transport=transport)
        assert args.transport == transport


class TestFilesystemToolBackwardCompatibility:
    """Test backward compatibility (tools work without transport)."""

    def test_fs_read_without_transport(self):
        """Test FsReadArgs works without transport parameter."""
        args = FsReadArgs(path="test.txt")
        assert args.transport is None

    def test_fs_write_without_transport(self):
        """Test FsWriteArgs works without transport parameter."""
        args = FsWriteArgs(path="test.txt", content="data")
        assert args.transport is None

    def test_fs_append_without_transport(self):
        """Test FsAppendArgs works without transport parameter."""
        args = FsAppendArgs(path="test.txt", content="data")
        assert args.transport is None

    def test_fs_delete_without_transport(self):
        """Test FsDeleteArgs works without transport parameter."""
        args = FsDeleteArgs(path="test.txt")
        assert args.transport is None

    def test_fs_make_dir_without_transport(self):
        """Test FsMakeDirArgs works without transport parameter."""
        args = FsMakeDirArgs(path="test_dir")
        assert args.transport is None

    def test_fs_list_without_transport(self):
        """Test FsListArgs works without transport parameter."""
        args = FsListArgs(path="test_dir")
        assert args.transport is None

    def test_fs_move_without_transport(self):
        """Test FsMoveArgs works without transport parameter."""
        args = FsMoveArgs(src="old.txt", dest="new.txt")
        assert args.transport is None

    def test_fs_copy_without_transport(self):
        """Test FsCopyArgs works without transport parameter."""
        args = FsCopyArgs(src="old.txt", dest="new.txt")
        assert args.transport is None

    def test_fs_stat_without_transport(self):
        """Test FsStatArgs works without transport parameter."""
        args = FsStatArgs(path="test.txt")
        assert args.transport is None

    def test_fs_find_without_transport(self):
        """Test FsFindArgs works without transport parameter."""
        args = FsFindArgs(filename_glob="*.txt")
        assert args.transport is None

    def test_fs_search_text_without_transport(self):
        """Test FsSearchTextArgs works without transport parameter."""
        args = FsSearchTextArgs(query="test")
        assert args.transport is None


class TestFilesystemToolDocumentation:
    """Test that documentation is complete."""

    def test_fs_read_has_transport_docs(self):
        """Test FsReadArgs has transport field documentation."""
        schema = FsReadArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field
        # Check that PTY is mentioned
        description = transport_field["description"].lower()
        assert "pty" in description or "transport" in description

    def test_fs_write_has_transport_docs(self):
        """Test FsWriteArgs has transport field documentation."""
        schema = FsWriteArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_append_has_transport_docs(self):
        """Test FsAppendArgs has transport field documentation."""
        schema = FsAppendArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_delete_has_transport_docs(self):
        """Test FsDeleteArgs has transport field documentation."""
        schema = FsDeleteArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_make_dir_has_transport_docs(self):
        """Test FsMakeDirArgs has transport field documentation."""
        schema = FsMakeDirArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_list_has_transport_docs(self):
        """Test FsListArgs has transport field documentation."""
        schema = FsListArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_move_has_transport_docs(self):
        """Test FsMoveArgs has transport field documentation."""
        schema = FsMoveArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_copy_has_transport_docs(self):
        """Test FsCopyArgs has transport field documentation."""
        schema = FsCopyArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_stat_has_transport_docs(self):
        """Test FsStatArgs has transport field documentation."""
        schema = FsStatArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_find_has_transport_docs(self):
        """Test FsFindArgs has transport field documentation."""
        schema = FsFindArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field

    def test_fs_search_text_has_transport_docs(self):
        """Test FsSearchTextArgs has transport field documentation."""
        schema = FsSearchTextArgs.model_json_schema()
        assert "transport" in schema["properties"]
        transport_field = schema["properties"]["transport"]
        assert "description" in transport_field


class TestFilesystemToolTransportValidation:
    """Test transport parameter validation (Phase 3.2.4.2)."""

    def test_invalid_transport_rejected(self):
        """Test that invalid transport values are rejected by Pydantic."""
        with pytest.raises(ValidationError):
            FsReadArgs(path="test.txt", transport="invalid")

    @pytest.mark.parametrize("schema_cls", SCHEMA_MAP.values())
    def test_direct_transport_rejected_by_schema(self, schema_cls):
        """Filesystem tools execute inside Kali and must not accept direct transport."""
        kwargs = {"transport": "direct"}
        if schema_cls is FsWriteArgs or schema_cls is FsAppendArgs:
            kwargs.update({"path": "test.txt", "content": "data"})
        elif schema_cls is FsMoveArgs or schema_cls is FsCopyArgs:
            kwargs.update({"src": "old.txt", "dest": "new.txt"})
        elif schema_cls is FsSearchTextArgs:
            kwargs.update({"query": "test"})
        elif schema_cls is FsFindArgs:
            kwargs.update({"filename_glob": "*.txt"})
        else:
            kwargs.update({"path": "test.txt"})

        with pytest.raises(ValidationError):
            schema_cls(**kwargs)

    def test_transport_is_optional(self):
        """Test that transport parameter is optional."""
        # Should not raise
        args = FsReadArgs(path="test.txt")
        assert args.transport is None

    def test_transport_none_is_valid(self):
        """Test that transport=None is explicitly valid."""
        args = FsReadArgs(path="test.txt", transport=None)
        assert args.transport is None


class TestFilesystemToolMetadataRegistration:
    """Test metadata registration for filesystem tools (Phase 3.2.4.3)."""

    @pytest.mark.parametrize("tool_id", FS_TOOLS)
    def test_tool_is_registered(self, tool_id):
        """Test that all filesystem tools are registered in metadata."""
        metadata = get_enhanced_tool_metadata(tool_id)
        assert metadata is not None, f"{tool_id} not registered in metadata"

    @pytest.mark.parametrize("tool_id", FS_TOOLS)
    def test_tool_has_supported_transports(self, tool_id):
        """Test that all tools have supported_transports field."""
        metadata = get_enhanced_tool_metadata(tool_id)
        assert metadata.supported_transports is not None, \
            f"{tool_id} missing supported_transports field"

    @pytest.mark.parametrize("tool_id", FS_TOOLS)
    def test_tool_supports_file_comm(self, tool_id):
        """Test that all filesystem tools support file-comm transport."""
        metadata = get_enhanced_tool_metadata(tool_id)
        assert "file-comm" in metadata.supported_transports, \
            f"{tool_id} missing 'file-comm' in supported_transports"

    @pytest.mark.parametrize("tool_id", FS_TOOLS)
    def test_tool_supports_pty(self, tool_id):
        """Test that all filesystem tools support PTY transport."""
        metadata = get_enhanced_tool_metadata(tool_id)
        assert "pty" in metadata.supported_transports, \
            f"{tool_id} missing 'pty' in supported_transports"

    @pytest.mark.parametrize("tool_id", FS_TOOLS)
    def test_tool_does_not_support_direct(self, tool_id):
        """Test that filesystem tools do NOT support direct Python I/O.
        
        Filesystem tools should only execute via file-comm or PTY,
        not through direct Python file I/O.
        """
        metadata = get_enhanced_tool_metadata(tool_id)
        assert "direct" not in metadata.supported_transports, \
            f"{tool_id} should not support 'direct' transport"

    @pytest.mark.parametrize("tool_id", FS_TOOLS)
    def test_tool_has_correct_category(self, tool_id):
        """Test that all filesystem tools have WORKSPACE_FILESYSTEM category."""
        metadata = get_enhanced_tool_metadata(tool_id)
        assert "WORKSPACE_FILESYSTEM" in str(metadata.category), \
            f"{tool_id} has incorrect category: {metadata.category}"
