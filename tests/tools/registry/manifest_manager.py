from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_MANIFEST_PATH = Path("tests") / "tools" / "registry" / "tool_test_manifest.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class ToolTestResult:
    tool_id: str
    test_name: str
    passed: bool
    timestamp: str


class ManifestManager:
    """Manage the tool testing manifest JSON file."""

    def __init__(self, manifest_path: Optional[Path] = None) -> None:
        self.manifest_path = manifest_path or DEFAULT_MANIFEST_PATH
        self._manifest_cache: Dict[str, Any] | None = None

    def load_manifest(self) -> Dict[str, Any]:
        if self._manifest_cache is not None:
            return self._manifest_cache

        if not self.manifest_path.exists():
            self._manifest_cache = self._create_empty_manifest()
            return self._manifest_cache

        raw = self.manifest_path.read_text(encoding="utf-8")
        self._manifest_cache = json.loads(raw)
        return self._manifest_cache

    def save_manifest(self) -> None:
        manifest = self.load_manifest()
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest["last_updated"] = _utc_now_iso()
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    def _create_empty_manifest(self) -> Dict[str, Any]:
        return {
            "version": "1.0.0",
            "last_updated": _utc_now_iso(),
            "tools": {},
            "categories": {},
        }

    def get_all_tools(self) -> List[str]:
        manifest = self.load_manifest()
        return sorted(manifest.get("tools", {}).keys())

    def get_tool_entry(self, tool_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest()
        return manifest.get("tools", {}).get(tool_id, {})

    def get_tools_by_category(self, category: str) -> List[str]:
        manifest = self.load_manifest()
        tools = manifest.get("tools", {})
        return sorted([tool_id for tool_id, data in tools.items() if data.get("category") == category])

    def get_untested_tools(self) -> List[str]:
        manifest = self.load_manifest()
        tools = manifest.get("tools", {})
        return sorted(
            [tool_id for tool_id, data in tools.items() if data.get("status") != "tested"]
        )

    def ensure_tool_entry(
        self,
        tool_id: str,
        *,
        category: str,
        subcategory: str,
        binary: Optional[str] = None,
    ) -> None:
        manifest = self.load_manifest()
        tools = manifest.setdefault("tools", {})
        if tool_id in tools:
            return

        tools[tool_id] = {
            "category": category,
            "subcategory": subcategory,
            "binary": binary or "",
            "status": "pending",
            "last_tested": None,
            "test_results": {},
            "fixture_available": False,
            "help_cached": False,
            "notes": "Pending fixture creation",
        }

    def update_tool_status(self, tool_id: str, status: str) -> None:
        manifest = self.load_manifest()
        tools = manifest.setdefault("tools", {})
        entry = tools.setdefault(tool_id, {})
        entry["status"] = status
        if status == "tested":
            entry["last_tested"] = _utc_now_iso()

    def update_tool_result(self, tool_id: str, test_name: str, passed: bool) -> ToolTestResult:
        manifest = self.load_manifest()
        tools = manifest.setdefault("tools", {})
        entry = tools.setdefault(tool_id, {})
        test_results = entry.setdefault("test_results", {})
        test_results[test_name] = "passed" if passed else "failed"

        if passed:
            status = entry.get("status", "pending")
            if status != "tested":
                entry["status"] = "partial"
        else:
            entry["status"] = "failed"

        entry["last_tested"] = _utc_now_iso()
        self._recalculate_category_counts()

        return ToolTestResult(
            tool_id=tool_id,
            test_name=test_name,
            passed=passed,
            timestamp=entry["last_tested"],
        )

    def update_fixture_status(self, tool_id: str, *, fixture_available: bool | None = None, help_cached: bool | None = None) -> None:
        manifest = self.load_manifest()
        entry = manifest.setdefault("tools", {}).setdefault(tool_id, {})
        if fixture_available is not None:
            entry["fixture_available"] = fixture_available
        if help_cached is not None:
            entry["help_cached"] = help_cached

    def bulk_register_tools(self, entries: Iterable[Dict[str, Any]]) -> None:
        for entry in entries:
            self.ensure_tool_entry(
                entry["tool_id"],
                category=entry["category"],
                subcategory=entry.get("subcategory", ""),
                binary=entry.get("binary"),
            )
        self._recalculate_category_counts()

    def _recalculate_category_counts(self) -> None:
        manifest = self.load_manifest()
        categories: Dict[str, Dict[str, int]] = {}
        for data in manifest.get("tools", {}).values():
            category = data.get("category", "unknown")
            status = data.get("status", "pending")
            if category not in categories:
                categories[category] = {"total": 0, "tested": 0, "pending": 0, "failed": 0, "partial": 0}
            categories[category]["total"] += 1
            if status in categories[category]:
                categories[category][status] += 1
            else:
                categories[category]["pending"] += 1
        manifest["categories"] = categories
