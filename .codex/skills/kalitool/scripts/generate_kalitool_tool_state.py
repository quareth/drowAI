"""Generate artifacts/kalitool-tool-state.md from the tool registry.

Lists each category (from agent/tools layout) and under each category every
discovered tool. Each tool is a checkbox line; when the kalitool-batch-tester
subagent completes a tool, it marks the checkbox. Running this script
(re)builds the list from the registry and optionally preserves existing
completion marks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from collections import defaultdict


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    return here.parents[4]


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.tools.tool_registry import available_tools, get_tool  # noqa: E402


def get_tools_by_category() -> dict[str, list[str]]:
    """Return {category: [tool_id, ...]} for loadable BaseTool modules only."""
    tool_ids: list[str] = []
    for tid in available_tools():
        try:
            get_tool(tid)
        except Exception:
            continue
        tool_ids.append(tid)

    by_cat: dict[str, list[str]] = defaultdict(list)
    for tid in tool_ids:
        if "." in tid:
            cat = tid.split(".")[0]
            by_cat[cat].append(tid)
    for cat in by_cat:
        by_cat[cat].sort()
    return dict(sorted(by_cat.items()))


def read_existing_marks(path: Path) -> set[str]:
    """Read tool state file and return set of tool_ids that are marked completed ([x])."""
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    completed = set()
    # Match lines like "- [x] `category.subcategory.tool_name`" or "- [x] category.subcategory.tool_name"
    for m in re.finditer(r"^\s*-\s+\[x\]\s+[`]?([a-zA-Z0-9_.]+)[`]?", text, re.MULTILINE | re.IGNORECASE):
        completed.add(m.group(1))
    return completed


def write_tool_state(path: Path, by_category: dict[str, list[str]], completed: set[str] | None = None) -> None:
    """Write markdown tool state file. If completed is set, use [x] for those tool_ids."""
    completed = completed or set()
    lines = [
        "# Kalitool batch test – tool state",
        "",
        "Generated from the tool registry. The kalitool-batch-tester subagent runs each tool",
        "with the kalitool skill (minimal and full schema parameters, safe placeholders only)",
        "and marks the checkbox when done.",
        "",
        "---",
        "",
    ]
    for category, tool_ids in by_category.items():
        lines.append(f"## {category}")
        lines.append("")
        for tid in tool_ids:
            checked = "[x]" if tid in completed else "[ ]"
            lines.append(f"- {checked} `{tid}`")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    out_path = ROOT / "artifacts" / "kalitool-tool-state.md"
    by_category = get_tools_by_category()
    completed = read_existing_marks(out_path)
    write_tool_state(out_path, by_category, completed=completed)
    total = sum(len(tools) for tools in by_category.values())
    print(f"[generate_kalitool_tool_state] Wrote {out_path} ({len(by_category)} categories, {total} tools, {len(completed)} already completed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
