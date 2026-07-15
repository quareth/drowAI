"""File browser service with workspace-bound path validation."""

from __future__ import annotations

import base64
import html
import json
import mimetypes
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from runtime_shared.workspace_filesystem import (
    WorkspaceEntry,
    WorkspaceFilesystem,
    normalize_workspace_relative_path,
)


@dataclass(frozen=True)
class _Limits:
    max_tree_depth: int = 10
    max_tree_entries: int = 2000
    max_search_results: int = 500
    search_timeout_seconds: float = 10.0


class _SafeHTMLSanitizer(HTMLParser):
    """Very small allowlist HTML sanitizer for markdown-rendered HTML."""

    _allowed_tags = {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
    _allowed_attrs = {
        "a": {"href", "title"},
    }
    _blocked_tags = {"script", "iframe", "object", "embed"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._result: list[str] = []
        self._skip_depth = 0

    def get_html(self) -> str:
        return "".join(self._result)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._blocked_tags:
            self._skip_depth += 1
            return
        if self._skip_depth > 0 or tag not in self._allowed_tags:
            return

        allowed = self._allowed_attrs.get(tag, set())
        clean_attrs: list[str] = []
        for key, value in attrs:
            key = (key or "").lower()
            if key.startswith("on"):
                continue
            if key not in allowed:
                continue
            if value is None:
                continue
            value = value.strip()
            if key == "href" and value.lower().startswith("javascript:"):
                continue
            clean_attrs.append(f'{key}="{html.escape(value, quote=True)}"')

        attr_suffix = ""
        if clean_attrs:
            attr_suffix = " " + " ".join(clean_attrs)
        self._result.append(f"<{tag}{attr_suffix}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._blocked_tags and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0 or tag not in self._allowed_tags:
            return
        self._result.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self._result.append(html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._result.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._skip_depth == 0:
            self._result.append(f"&#{name};")


class FileBrowserService:
    """Business logic for file browsing under a task workspace."""

    def __init__(self, limits: _Limits | None = None) -> None:
        self._limits = limits or _Limits()

    def validate_path(self, workspace_path: Path, user_path: str | None) -> Path:
        workspace = workspace_path
        raw = (user_path or "").strip()

        if not raw or raw in {".", "/"}:
            return workspace
        relative = normalize_workspace_relative_path(raw.lstrip("/\\"))
        return workspace / relative

    @staticmethod
    def _relative_path(user_path: str | None) -> str | None:
        raw = (user_path or "").strip()
        if not raw or raw in {".", "/"}:
            return None
        return normalize_workspace_relative_path(raw.lstrip("/\\"))

    def get_directory_tree_at_workspace(
        self,
        *,
        workspace_path: Path,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Return a directory tree under an already provider-resolved workspace."""
        relative_root = self._relative_path(path)
        entries: list[WorkspaceEntry] = []
        root_depth = len(PurePosixPath(relative_root).parts) if relative_root else 0
        for entry in WorkspaceFilesystem(workspace_path).iter_entries(
            relative_root, recursive=True
        ):
            if len(entries) + 2 > self._limits.max_tree_entries:
                raise ValueError("Directory entry limit exceeded.")
            depth = len(PurePosixPath(entry.relative_path).parts) - root_depth
            if depth > self._limits.max_tree_depth:
                raise ValueError("Directory depth limit exceeded.")
            entries.append(entry)
        return self._build_tree_from_entries(
            workspace_path,
            relative_root=relative_root,
            entries=tuple(entries),
        )

    def get_file_content_at_workspace(
        self,
        *,
        workspace_path: Path,
        file_path: str,
    ) -> dict[str, Any]:
        """Return sanitized preview content from a provider-resolved workspace."""
        relative_path = self._relative_path(file_path)
        if relative_path is None:
            raise ValueError("Path points to a directory, not a file.")
        filesystem = WorkspaceFilesystem(workspace_path)
        entry = filesystem.metadata(relative_path)
        file_bytes = filesystem.read_bytes(relative_path)
        size = len(file_bytes)
        modified = self._format_timestamp(entry.modified_at)
        output_path = f"/{relative_path}"
        name = Path(relative_path).name
        mime_type, _ = mimetypes.guess_type(name)
        metadata = {
            "is_valid_json": None,
            "is_valid_xml": None,
            "line_count": None,
        }

        try:
            decoded = file_bytes.decode("utf-8")
            preview_type = self._preview_type_from_name(name)
            if preview_type == "markdown":
                content = self._sanitize_markdown(decoded)
            elif preview_type == "json":
                content, metadata["is_valid_json"] = self._sanitize_json(decoded)
            elif preview_type == "xml":
                content, metadata["is_valid_xml"] = self._sanitize_xml(decoded)
            else:
                content = self._sanitize_text(decoded)
                preview_type = "text"
            metadata["line_count"] = self._line_count(decoded)
            encoding = "utf-8"
            file_type = mime_type or "text/plain"
        except UnicodeDecodeError:
            content = base64.b64encode(file_bytes).decode("ascii")
            preview_type = "binary"
            encoding = "base64"
            file_type = mime_type or "application/octet-stream"

        return {
            "path": output_path,
            "name": name,
            "size": size,
            "type": file_type,
            "content": content,
            "encoding": encoding,
            "preview_type": preview_type,
            "is_truncated": False,
            "modified": modified,
            "metadata": metadata,
        }

    def build_text_file_content_response(
        self,
        *,
        file_path: str,
        raw_content: str,
        size: int | None = None,
        modified: str | None = None,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        """Build the file-content API response from provider-returned text."""
        normalized_path = self._normalize_relative_output_path(file_path)
        name = Path(normalized_path.lstrip("/") or file_path).name
        mime_type, _ = mimetypes.guess_type(name)
        metadata = {
            "is_valid_json": None,
            "is_valid_xml": None,
            "line_count": None,
        }
        preview_type = self._preview_type_from_name(name)
        if preview_type == "markdown":
            content = self._sanitize_markdown(raw_content)
            file_type = mime_type or "text/markdown"
        elif preview_type == "json":
            content, metadata["is_valid_json"] = self._sanitize_json(raw_content)
            file_type = mime_type or "application/json"
        elif preview_type == "xml":
            content, metadata["is_valid_xml"] = self._sanitize_xml(raw_content)
            file_type = mime_type or "application/xml"
        else:
            content = self._sanitize_text(raw_content)
            preview_type = "text"
            file_type = mime_type or "text/plain"
        metadata["line_count"] = self._line_count(raw_content)
        encoded_size = len(raw_content.encode(encoding or "utf-8", errors="replace"))
        return {
            "path": normalized_path,
            "name": name,
            "size": int(size if size is not None else encoded_size),
            "type": file_type,
            "content": content,
            "encoding": encoding or "utf-8",
            "preview_type": preview_type,
            "is_truncated": False,
            "modified": modified or self._format_timestamp(datetime.now(tz=timezone.utc).timestamp()),
            "metadata": metadata,
        }

    def search_files_at_workspace(
        self,
        *,
        workspace_path: Path,
        query: str,
        path: str | None = None,
    ) -> dict[str, Any]:
        """Search files under a provider-resolved workspace."""
        relative_root = self._relative_path(path)
        query_lower = (query or "").lower().strip()
        deadline = time.monotonic() + self._limits.search_timeout_seconds
        results: list[dict[str, Any]] = []
        total_count = 0
        truncated = False
        entries = WorkspaceFilesystem(workspace_path).iter_entries(
            relative_root, recursive=True
        )
        for entry in entries:
            if time.monotonic() > deadline:
                truncated = True
                break
            if entry.kind != "file":
                continue
            name = Path(entry.relative_path).name
            if query_lower and query_lower not in name.lower():
                continue
            total_count += 1
            results.append(
                {
                    "name": name,
                    "type": "file",
                    "path": f"/{entry.relative_path}",
                    "size": entry.size,
                    "modified": self._format_timestamp(entry.modified_at),
                }
            )
            if len(results) >= self._limits.max_search_results:
                truncated = True
                break

        return {
            "query": query,
            "results": results,
            "total_count": total_count,
            "truncated": truncated,
        }

    def create_zip_archive_at_workspace(
        self,
        *,
        workspace_path: Path,
        file_paths: list[str],
    ) -> Path:
        """Create a ZIP archive from paths under a provider-resolved workspace."""
        if not file_paths:
            raise ValueError("At least one path is required to create an archive.")
        filesystem = WorkspaceFilesystem(workspace_path)
        relative_paths: list[str] = []
        for raw_path in file_paths:
            relative = self._relative_path(raw_path)
            if relative is None:
                relative_paths.extend(
                    entry.relative_path
                    for entry in filesystem.list_entries(recursive=False)
                )
            else:
                relative_paths.append(relative)
        relative_paths = list(dict.fromkeys(relative_paths))
        if relative_paths:
            return filesystem.create_zip(relative_paths)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_file:
            zip_path = Path(temp_file.name)
        with zipfile.ZipFile(zip_path, "w"):
            pass
        return zip_path

    def snapshot_download_at_workspace(
        self,
        *,
        workspace_path: Path,
        file_path: str,
    ) -> Path:
        """Snapshot one safe workspace file for response-time streaming."""
        relative_path = self._relative_path(file_path)
        if relative_path is None:
            raise ValueError("Path points to a directory, not a file.")
        return WorkspaceFilesystem(workspace_path).snapshot_to_temp(relative_path)

    def _build_tree_from_entries(
        self,
        workspace: Path,
        *,
        relative_root: str | None,
        entries: tuple[WorkspaceEntry, ...],
    ) -> dict[str, Any]:
        if len(entries) + 1 > self._limits.max_tree_entries:
            raise ValueError("Directory entry limit exceeded.")
        root_path = f"/{relative_root}" if relative_root else "/"
        root: dict[str, Any] = {
            "name": Path(relative_root).name if relative_root else workspace.name,
            "type": "folder",
            "path": root_path,
            "size": None,
            "modified": self._format_timestamp(datetime.now(tz=timezone.utc).timestamp()),
            "children": [],
        }
        nodes: dict[str, dict[str, Any]] = {relative_root or "": root}
        root_depth = len(Path(relative_root).parts) if relative_root else 0
        for entry in sorted(entries, key=lambda item: (item.relative_path.count("/"), item.relative_path)):
            depth = len(Path(entry.relative_path).parts) - root_depth
            if depth > self._limits.max_tree_depth:
                raise ValueError("Directory depth limit exceeded.")
            parent = PurePosixPath(entry.relative_path).parent
            parent_path = "" if str(parent) == "." else parent.as_posix()
            parent = nodes.get(parent_path)
            if parent is None:
                raise ValueError("Workspace directory tree changed during access.")
            node = {
                "name": Path(entry.relative_path).name,
                "type": "folder" if entry.kind == "directory" else "file",
                "path": f"/{entry.relative_path}",
                "size": None if entry.kind == "directory" else entry.size,
                "modified": self._format_timestamp(entry.modified_at),
                "children": [],
            }
            parent["children"].append(node)
            if entry.kind == "directory":
                nodes[entry.relative_path] = node
        for node in nodes.values():
            node["children"].sort(key=lambda item: (item["type"] != "folder", item["name"].lower()))
        return root

    @staticmethod
    def _line_count(content: str) -> int:
        if not content:
            return 0
        return content.count("\n") + 1

    @staticmethod
    def _format_timestamp(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

    @staticmethod
    def _preview_type_from_name(file_name: str) -> str:
        suffix = Path(file_name).suffix.lower()
        if suffix in {".md", ".markdown"}:
            return "markdown"
        if suffix == ".json":
            return "json"
        if suffix in {".xml", ".svg"}:
            return "xml"
        return "text"

    @staticmethod
    def _to_workspace_relative(workspace: Path, value: Path) -> str:
        if value == workspace:
            return "/"
        rel = value.relative_to(workspace).as_posix()
        return f"/{rel}"

    @staticmethod
    def _normalize_relative_output_path(path: str) -> str:
        normalized = str(path or "").strip().replace("\\", "/").lstrip("/")
        if not normalized or normalized == ".":
            return "/"
        return "/" + normalized.rstrip("/")

    @staticmethod
    def _to_zip_relative(workspace: Path, value: Path) -> str:
        return value.relative_to(workspace).as_posix()

    @staticmethod
    def _sanitize_text(content: str) -> str:
        return html.escape(content)

    def _sanitize_markdown(self, content: str) -> str:
        rendered = self._render_markdown(content)
        parser = _SafeHTMLSanitizer()
        parser.feed(rendered)
        parser.close()
        return parser.get_html()

    @staticmethod
    def _render_markdown(content: str) -> str:
        try:
            import markdown as md  # type: ignore

            return md.markdown(content, extensions=["fenced_code", "tables"])
        except Exception:
            return content

    def _sanitize_json(self, content: str) -> tuple[str, bool]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return html.escape(content), False

        def escape_json_values(value: Any) -> Any:
            if isinstance(value, str):
                return html.escape(value)
            if isinstance(value, list):
                return [escape_json_values(item) for item in value]
            if isinstance(value, dict):
                return {key: escape_json_values(item) for key, item in value.items()}
            return value

        safe = escape_json_values(parsed)
        return json.dumps(safe, indent=2, ensure_ascii=False), True

    def _sanitize_xml(self, content: str) -> tuple[str, bool]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return html.escape(content), False

        self._escape_xml_tree(root)
        self._indent_xml(root)
        return ET.tostring(root, encoding="unicode"), True

    def _escape_xml_tree(self, node: ET.Element) -> None:
        blocked = {"script", "iframe", "object", "embed"}
        for child in list(node):
            if child.tag.lower() in blocked:
                node.remove(child)
                continue
            self._escape_xml_tree(child)

        if node.text:
            node.text = html.escape(node.text)
        if node.tail:
            node.tail = html.escape(node.tail)
        if node.attrib:
            node.attrib = {key: html.escape(value) for key, value in node.attrib.items()}

    @staticmethod
    def _indent_xml(root: ET.Element) -> None:
        try:
            ET.indent(root, space="  ")
        except AttributeError:
            pass
