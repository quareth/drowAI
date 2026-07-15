from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from agent.tools.tool_registry import available_tools, get_tool

from .manifest_manager import ManifestManager


@dataclass(slots=True)
class DiscoveredTool:
    tool_id: str
    category: str
    subcategory: str
    binary: str


class ToolScanner:
    """Discover tool modules and sync with the testing manifest."""

    def __init__(self, manifest: ManifestManager | None = None) -> None:
        self.manifest = manifest or ManifestManager()
        self.params_dir = Path("tests") / "tools" / "fixtures" / "params"

    def discover_tools(self) -> List[DiscoveredTool]:
        discovered: List[DiscoveredTool] = []
        for tool_id in available_tools():
            try:
                tool_cls = get_tool(tool_id)
            except Exception:
                continue

            category, subcategory = self._extract_category(tool_id)
            binary = self._infer_binary_name(tool_id, tool_cls.__name__)
            discovered.append(
                DiscoveredTool(
                    tool_id=tool_id,
                    category=category,
                    subcategory=subcategory,
                    binary=binary,
                )
            )
        return discovered

    def find_new_tools(self) -> List[str]:
        discovered = {tool.tool_id for tool in self.discover_tools()}
        existing = set(self.manifest.get_all_tools())
        return sorted(discovered - existing)

    def sync_manifest(self) -> List[str]:
        discovered = self.discover_tools()
        existing = set(self.manifest.get_all_tools())
        new_entries = [tool for tool in discovered if tool.tool_id not in existing]

        if new_entries:
            self.manifest.bulk_register_tools(
                [
                    {
                        "tool_id": tool.tool_id,
                        "category": tool.category,
                        "subcategory": tool.subcategory,
                        "binary": tool.binary,
                    }
                    for tool in new_entries
                ]
            )
            for tool in new_entries:
                self._ensure_fixture_skeleton(tool.tool_id)
            self.manifest.save_manifest()

        return [tool.tool_id for tool in new_entries]

    def _extract_category(self, tool_id: str) -> tuple[str, str]:
        parts = tool_id.split(".")
        if not parts:
            return "unknown", ""
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[1]

    def _infer_binary_name(self, tool_id: str, class_name: str) -> str:
        slug = tool_id.split(".")[-1]
        if slug:
            return slug.replace("_", "-")
        return class_name.replace("Tool", "").lower()

    def _ensure_fixture_skeleton(self, tool_id: str) -> Path:
        self.params_dir.mkdir(parents=True, exist_ok=True)
        fixture_path = self.params_dir / f"{tool_id.replace('.', '_')}.json"
        if fixture_path.exists():
            return fixture_path

        content: Dict[str, object] = {
            "tool_id": tool_id,
            "test_cases": {
                "minimal": {
                    "description": "Required fields only",
                    "params": {"target": "127.0.0.1"},
                    "expected_valid": True,
                },
                "full": {
                    "description": "All optional fields populated",
                    "params": {"target": "127.0.0.1"},
                    "expected_valid": True,
                },
                "edge_cases": [],
                "invalid": [],
            },
            "expected_command_patterns": {
                "minimal": [],
                "full": [],
            },
        }
        fixture_path.write_text(
            __import__("json").dumps(content, indent=2),
            encoding="utf-8",
        )
        return fixture_path
