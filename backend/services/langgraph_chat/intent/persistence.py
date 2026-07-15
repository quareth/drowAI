"""Persist intent metadata as management-plane chat state."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from agent.graph.state import InteractiveState

from backend.core.time_utils import format_iso, utc_now
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig

logger = logging.getLogger("backend.services.langgraph_chat.intent_persistence")


def _load_state_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            content = handle.read().strip()
            if not content:
                # Empty file, return empty dict
                logger.debug(
                    "agent_state.json at %s is empty, returning empty dict", path
                )
                return {}
            payload = json.loads(content)
            return payload if isinstance(payload, dict) else {}
    except Exception:
        logger.warning("Failed to load agent_state.json from %s", path, exc_info=True)
        return {}


def _write_state_file(path: Path, data: Dict[str, Any]) -> None:
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except Exception:
        logger.warning("Failed to persist intent state to %s", path, exc_info=True)


def _resolve_management_intent_state_path(task_id: int) -> Path:
    """Resolve task-scoped management-plane intent state path."""
    project_root = Path(__file__).resolve().parents[4]
    state_root = (
        project_root
        / "backend"
        / "management_state"
        / "langgraph_chat_intent"
        / f"task-{int(task_id)}"
    )
    state_root.mkdir(parents=True, exist_ok=True)
    return state_root / "agent_state.json"


def persist_intent_context(
    runtime_config: LangGraphRuntimeConfig,
    state: InteractiveState,
) -> None:
    """Persist intent routing context into management-plane durable state."""

    try:
        task_id = int(runtime_config.chat_inputs.task_id)
        state_path = _resolve_management_intent_state_path(task_id)

        data = _load_state_file(state_path)
        history = data.setdefault("intent_history", [])
        if not isinstance(history, list):
            history = data["intent_history"] = []

        entry = {
            "timestamp": format_iso(utc_now()),
            "conversation_id": runtime_config.chat_inputs.conversation_id,
            "message": runtime_config.chat_inputs.message,
            "capability": state.facts.capability,
            "intent_hints": state.facts.intent_hints,
            "risk_flags": state.facts.risk_flags,
            "router": state.facts.metadata.get("intent_router")
            if state.facts.metadata
            else {},
        }
        history.append(entry)
        _write_state_file(state_path, data)
    except Exception:
        logger.warning(
            "Failed to persist intent context for task %s",
            runtime_config.chat_inputs.task_id,
            exc_info=True,
        )


__all__ = ["persist_intent_context"]
