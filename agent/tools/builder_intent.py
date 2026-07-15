"""Reserved builder-intent meta-field shared by spec building and resolution.

The planner's native tool-call builder is the only LLM that sees the committed
downstream context and decides what each specific call is for. We capture that
per-call intent through a reserved property (``_builder_intent``) injected into
every function schema, then strip it before tool-parameter validation so the
tool itself never receives it.

This module is a dependency-free leaf so both the spec builder
(``agent.tools.tool_call_specs``) and the parameter resolver
(``agent.reasoning``) can share one definition without import cycles.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Tuple

BUILDER_INTENT_KEY = "_builder_intent"

BUILDER_INTENT_PROPERTY_SCHEMA: Dict[str, Any] = {
    "type": "string",
    "description": (
        "One short sentence: what this call is trying to learn or achieve, and—if "
        "relevant—what decision-critical signals you expect in the tool output "
        "(e.g. open ports, HTTP status, module success, session opened, error cause). "
        "Not a tool parameter."
    ),
}


def inject_builder_intent_property(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """Add the reserved intent property to an object schema, in place.

    No-op for non-object schemas, schemas without a ``properties`` map, or
    schemas that already declare the key. When a ``required`` list exists the
    key is appended so providers reliably emit it; schemas without a
    ``required`` list are left as optional to avoid over-constraining tools.
    """
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        return parameters
    props = parameters.get("properties")
    if not isinstance(props, dict) or BUILDER_INTENT_KEY in props:
        return parameters
    props[BUILDER_INTENT_KEY] = dict(BUILDER_INTENT_PROPERTY_SCHEMA)
    required = parameters.get("required")
    if isinstance(required, list) and BUILDER_INTENT_KEY not in required:
        required.append(BUILDER_INTENT_KEY)
    return parameters


def split_builder_intent(raw_arguments: Any) -> Tuple[Any, str]:
    """Return ``(parameters_without_intent, intent)`` from builder arguments.

    Accepts the JSON-string or decoded-dict shapes the builder may produce. On
    any non-object payload or decode failure the original payload is returned
    untouched with an empty intent, so existing parse-error handling downstream
    stays unchanged.
    """
    if isinstance(raw_arguments, str):
        if not raw_arguments.strip():
            return raw_arguments, ""
        try:
            decoded = json.loads(raw_arguments)
        except Exception:
            return raw_arguments, ""
    elif isinstance(raw_arguments, dict):
        decoded = dict(raw_arguments)
    else:
        return raw_arguments, ""

    if not isinstance(decoded, dict):
        return raw_arguments, ""

    intent = decoded.pop(BUILDER_INTENT_KEY, "")
    return decoded, intent if isinstance(intent, str) else ""


__all__ = [
    "BUILDER_INTENT_KEY",
    "BUILDER_INTENT_PROPERTY_SCHEMA",
    "inject_builder_intent_property",
    "split_builder_intent",
]
