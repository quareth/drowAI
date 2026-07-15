"""Shared tool interface contract consumed by LangGraph execution nodes."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Sequence


class ToolInterface(Protocol):
    """Protocol describing the behaviour required from simple-tool executors."""

    def get_args_for_non_tool_llm(
        self,
        query: str,
        history: Sequence[Mapping[str, Any]],
        llm: Any,
    ) -> Mapping[str, Any]:
        """Return normalized tool arguments when no tool-specific LLM is available."""

    def run(self, **kwargs: Any) -> Iterable[str]:
        """Execute the tool and yield streaming fragments (stdout/stderr/etc.)."""

    def final_result(self, *responses: str) -> Mapping[str, Any]:
        """Aggregate streamed responses into a structured result payload."""

    def build_next_prompt(self, result: Mapping[str, Any]) -> Optional[str]:
        """Optionally build a follow-up prompt when further clarification is needed."""


def normalize_tool_arguments(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Coerce LLM-produced tool arguments into JSON-serialisable dictionaries."""

    def _coerce(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            try:
                return value.model_dump()
            except Exception:
                return dict(value)
        if isinstance(value, dict):
            return {str(k): _coerce(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_coerce(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        try:
            return str(value)
        except Exception:
            return repr(value)

    return {str(key): _coerce(val) for key, val in raw.items()}


__all__ = ["ToolInterface", "normalize_tool_arguments"]
