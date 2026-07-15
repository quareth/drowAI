"""Dynamic registry for penetration testing tools."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - avoid circular import at runtime
    from .base_tool import BaseTool
    from .schemas import ToolResult

# Internal mapping of tool name to loaded class
_TOOLS: Dict[str, Type["BaseTool"]] = {}
# Mapping of tool name to module path for lazy import
_MODULE_INDEX: Dict[str, str] = {}
# Mapping of tool id to the concrete class name inside its module. This keeps
# multi-tool modules executable under their class-declared ids.
_CLASS_INDEX: Dict[str, str] = {}

# Per-process cache for catalog metadata (warm path)
_CATALOG_METADATA_CACHE: Optional[Dict[str, Dict[str, Any]]] = None

def _base_name(base: ast.expr) -> str:
    """Return the right-most base-class name from an AST expression."""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return ""


def _class_tool_id(node: ast.ClassDef, fallback: str) -> str:
    """Extract a class-declared tool_id, falling back to the module id."""
    for item in node.body:
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(item, ast.Assign):
            value = item.value
            targets = list(item.targets)
        elif isinstance(item, ast.AnnAssign):
            value = item.value
            targets = [item.target]

        if value is None:
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "tool_id"
            for target in targets
        ):
            continue
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and value.value.strip()
        ):
            return value.value.strip()
    return fallback


def _discover_tool_classes(path: Path, fallback_tool_id: str) -> Dict[str, str]:
    """Return executable tool ids declared by a Python module.

    The catalog must expose executable ``BaseTool`` subclasses, not helper
    modules. This AST pass preserves lazy imports while preventing helpers such
    as parsers, policies, and semantic modules from entering ``available_tools``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}

    discovered: Dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(_base_name(base) == "BaseTool" for base in node.bases):
            continue
        tool_id = _class_tool_id(node, fallback_tool_id)
        discovered[tool_id] = node.name
    return discovered


def _scan_for_tools() -> None:
    """Populate ``_MODULE_INDEX`` by scanning the tools directory."""
    root = Path(__file__).parent

    # Utility files and helpers that should NOT be treated as tools
    excluded_stems = {
        "base_tool",
        "schemas",
        "exceptions",
        "tool_registry",
        "utils",
        "parameter_validation",
        "action_mapper",
        "categories",
        "compatibility",
        "tool_call_specs",
        "parameter_generator",
        "service_matcher",
        "enhanced_metadata_registry",
        "enhanced_tool_metadata",
        "resolve_tools",
        "_helpers",
    }

    for path in root.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        if path.stem.startswith("_"):
            continue
        if path.stem in excluded_stems:
            continue

        rel = path.relative_to(root).with_suffix("")
        if "deprecated" in rel.parts:
            continue
        name = ".".join(rel.parts)
        module = f"agent.tools.{name}"
        for tool_id, class_name in _discover_tool_classes(path, name).items():
            _MODULE_INDEX[tool_id] = module
            _CLASS_INDEX[tool_id] = class_name


_scan_for_tools()


def register_tool(name: str, tool_cls: Type["BaseTool"]) -> None:
    """Register a tool class with a short name."""
    _TOOLS[name] = tool_cls


def tool_exists(name: str) -> bool:
    """Return True if a tool module or registered tool exists."""
    if name in _TOOLS or name in _MODULE_INDEX:
        return True
    try:
        spec = importlib.util.find_spec(f"agent.tools.{name}")
    except ModuleNotFoundError:
        return False
    if spec is None or not spec.origin:
        return False
    return name in _discover_tool_classes(Path(spec.origin), name)


def get_tool(name: str) -> Type["BaseTool"]:
    """Load and return the tool class with the given name."""
    if name in _TOOLS:
        return _TOOLS[name]

    module_path = _MODULE_INDEX.get(name)
    if module_path is None:
        spec = importlib.util.find_spec(f"agent.tools.{name}")
        if spec is None:
            raise ValueError(f"Tool '{name}' not found")
        discovered = (
            _discover_tool_classes(Path(spec.origin), name)
            if spec.origin
            else {}
        )
        if name not in discovered:
            raise ValueError(f"Tool '{name}' not found")
        module_path = f"agent.tools.{name}"
        _MODULE_INDEX[name] = module_path
        _CLASS_INDEX[name] = discovered[name]

    spec = importlib.util.find_spec(module_path)
    if spec is None:
        raise ValueError(f"Tool '{name}' not found")

    from .base_tool import BaseTool  # Imported lazily to avoid circular import

    module = importlib.import_module(module_path)
    class_name = _CLASS_INDEX.get(name)
    if class_name:
        obj = getattr(module, class_name, None)
        if isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool:
            register_tool(name, obj)
            return obj
        raise ValueError(f"Tool '{name}' has no BaseTool subclass")

    for attr in dir(module):
        obj = getattr(module, attr)
        if isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool:
            register_tool(name, obj)
            return obj

    raise ValueError(f"Tool '{name}' has no BaseTool subclass")


def run_tool_by_name(name: str, data: Dict[str, Any]) -> 'ToolResult':
    """Convenience helper to run a tool given its registry name."""
    tool_cls = get_tool(name)
    tool = tool_cls()
    from .utils import validate_and_execute_tool

    return validate_and_execute_tool(tool, data)


def available_tools() -> List[str]:
    """Return a sorted list of discovered tool names."""
    return sorted(set(_MODULE_INDEX) | set(_TOOLS))


def get_tool_metadata(name: str) -> Dict[str, Any]:
    """Return basic metadata describing a tool."""
    cls = get_tool(name)
    return {
        "name": name,
        "description": inspect.getdoc(cls) or "",
        "args_schema": cls.args_model.model_json_schema(),
    }


def get_catalog_metadata_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return metadata for all tools. Cached per process for warm-path lookups.
    
    First call does full cold scan (imports all tool modules). Subsequent
    calls return cached snapshot, avoiding metadata scan on post-approval
    dispatch hot path.
    """
    global _CATALOG_METADATA_CACHE
    if _CATALOG_METADATA_CACHE is not None:
        return _CATALOG_METADATA_CACHE
    tool_ids = available_tools()
    result: Dict[str, Dict[str, Any]] = {}
    for tid in tool_ids:
        try:
            result[tid] = get_tool_metadata(tid)
        except Exception:
            result[tid] = {"name": tid, "description": "", "category": ""}
    _CATALOG_METADATA_CACHE = result
    return result


def warm_catalog_metadata_snapshot() -> int:
    """Prime and retain cached catalog metadata for warm dispatch paths."""
    snapshot = get_catalog_metadata_snapshot()
    return len(snapshot)


def clear_catalog_metadata_cache() -> None:
    """Clear the catalog metadata cache. For tests that need fresh metadata."""
    global _CATALOG_METADATA_CACHE
    _CATALOG_METADATA_CACHE = None
