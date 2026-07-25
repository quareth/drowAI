"""Collect read-only registry, schema, planner, and visibility facts for one tool."""

from __future__ import annotations

import argparse
import json
from types import ModuleType
from typing import Any, Mapping, Sequence


def _schema_summary(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable summary without copying descriptions or sensitive values."""

    raw_properties = schema.get("properties")
    properties = raw_properties if isinstance(raw_properties, Mapping) else {}
    raw_required = schema.get("required")
    required = sorted(
        str(item) for item in raw_required
    ) if isinstance(raw_required, Sequence) and not isinstance(raw_required, str) else []
    fields: list[dict[str, Any]] = []
    for name in sorted(str(key) for key in properties):
        field = properties.get(name)
        field_map = field if isinstance(field, Mapping) else {}
        fields.append(
            {
                "name": name,
                "required": name in required,
                "has_default": "default" in field_map,
                "type": field_map.get("type"),
            }
        )
    return {"required": required, "fields": fields}


def _diagnostic(exc: BaseException) -> str:
    """Return only an exception class so diagnostics cannot echo secrets."""

    return type(exc).__name__


def collect_tool_capability_inputs(
    tool_id: str,
    *,
    registry: ModuleType | Any | None = None,
    visibility: ModuleType | Any | None = None,
    tool_specs: ModuleType | Any | None = None,
) -> dict[str, Any]:
    """Return bounded metadata for one exact tool ID without writing state."""

    normalized = str(tool_id or "").strip()
    if not normalized:
        raise ValueError("tool_id is required")

    diagnostics: list[str] = []
    if registry is None:
        try:
            from agent.tools import tool_registry as registry_module

            registry = registry_module
        except Exception as exc:  # import failure must remain bounded
            return {
                "tool_id": normalized,
                "registered": False,
                "llm_visible": False,
                "execution_schema": None,
                "planner_schema": None,
                "diagnostics": [f"registry_import:{_diagnostic(exc)}"],
            }

    available = set(registry.available_tools())
    registered = normalized in available
    result: dict[str, Any] = {
        "tool_id": normalized,
        "registered": registered,
        "llm_visible": False,
        "execution_schema": None,
        "planner_schema": None,
        "diagnostics": diagnostics,
    }
    if not registered:
        diagnostics.append("tool_not_registered")
        return result

    try:
        metadata = registry.get_tool_metadata(normalized)
        schema = metadata.get("args_schema", {}) if isinstance(metadata, Mapping) else {}
        result["execution_schema"] = _schema_summary(
            schema if isinstance(schema, Mapping) else {}
        )
    except Exception as exc:
        diagnostics.append(f"execution_schema:{_diagnostic(exc)}")

    if visibility is None:
        try:
            from agent.tools import catalog_visibility as visibility_module

            visibility = visibility_module
        except Exception as exc:
            diagnostics.append(f"visibility_import:{_diagnostic(exc)}")
    if visibility is not None:
        try:
            result["llm_visible"] = bool(
                visibility.is_tool_visible_in_catalog(normalized)
            )
        except Exception as exc:
            diagnostics.append(f"visibility:{_diagnostic(exc)}")

    if tool_specs is None:
        try:
            from agent.tools import tool_call_specs as tool_specs_module

            tool_specs = tool_specs_module
        except Exception as exc:
            diagnostics.append(f"planner_import:{_diagnostic(exc)}")
    if tool_specs is not None:
        try:
            spec = tool_specs.build_function_tool_spec_for(normalized)
            schema = getattr(spec, "parameters_schema", {})
            result["planner_schema"] = _schema_summary(
                schema if isinstance(schema, Mapping) else {}
            )
            result["planner_function_name"] = getattr(spec, "name", "")
        except Exception as exc:
            diagnostics.append(f"planner_schema:{_diagnostic(exc)}")

    return result


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(
        description="Print read-only DrowAI tool capability inputs as JSON."
    )
    parser.add_argument("--tool-id", required=True, help="Exact registered tool ID")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the collector and return a process exit status."""

    args = build_parser().parse_args(argv)
    payload = collect_tool_capability_inputs(args.tool_id)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["registered"] and payload["execution_schema"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
