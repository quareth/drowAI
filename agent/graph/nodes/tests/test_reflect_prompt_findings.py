"""Node-level coverage for the canonical reflect findings selector.

The Phase 2 cutover deleted the parallel reflect findings path
(``_build_reflection_relevant_findings_text`` and the
``select_relevant_findings_for_prompt(..., limit=6)`` call in
``agent/graph/nodes/reflect.py``). The reflect node now consumes the
canonical selector ``agent.graph.memory.findings.build_relevant_findings_for_prompt``
(``limit=8``, subject hints include ``tool_params``) — the same
selector think_more / synthesis use.

Comprehensive builder-level rendering of ``## Relevant Prior Findings``
lives in ``agent/graph/tests/test_reflect_prompt_context.py`` (added
in Task 2.4). This module focuses on the node-level wiring contract
that ownership-checklist item ``parallel-findings-path-removed`` and
``canonical-projections-only`` describe:

- ``_build_reflection_prompt`` calls ``build_relevant_findings_for_prompt``
  with the active ``InteractiveState`` (the interactive view, not the
  graph-state mapping) and threads the resulting list into
  ``build_reflection_prompt`` as the ``relevant_findings`` kwarg.
- The canonical selector's output is rendered under ``## Relevant Prior
  Findings`` in the final reflect prompt body (no parallel-path text).
- The legacy bold-line heading ``**Relevant Prior Findings**:`` no
  longer appears anywhere in the reflect prompt.
"""

from __future__ import annotations

from typing import Any, Mapping

from agent.graph.nodes.reflect import _build_reflection_prompt
from agent.graph.state import FactsState, InteractiveState, TraceState


def test_build_reflection_prompt_uses_canonical_findings_helper(monkeypatch) -> None:
    """``_build_reflection_prompt`` must call the canonical selector.

    Spies on
    ``agent.graph.nodes.reflect.build_relevant_findings_for_prompt`` to
    confirm the helper is invoked with the live ``InteractiveState``
    (not the ``as_graph_state()`` mapping) and that the sentinel return
    value is threaded into ``build_reflection_prompt`` and rendered
    under ``## Relevant Prior Findings``.
    """
    sentinel = [
        {
            "kind": "port_open",
            "target": "10.0.0.5",
            "subject": "10.0.0.5:22/tcp",
            "details": {"service": "ssh"},
            "assertion_level": "observed",
            "state": "fresh",
        }
    ]
    captured: dict[str, Any] = {}

    def fake_helper(interactive_arg: Any) -> list[Mapping[str, Any]]:
        captured["called_with"] = interactive_arg
        captured["call_count"] = captured.get("call_count", 0) + 1
        return sentinel

    monkeypatch.setattr(
        "agent.graph.nodes.reflect.build_relevant_findings_for_prompt",
        fake_helper,
    )

    interactive = InteractiveState(
        facts=FactsState(
            task_id=101,
            message="Continue enumerating",
            current_goal="Identify open ports on 10.0.0.5",
        ),
        trace=TraceState(),
    )

    prompt = _build_reflection_prompt(
        interactive,
        "Stuck in loop: repeated the same action 3 times without progress",
        None,
    )

    # The canonical helper was called exactly once with the live
    # InteractiveState (NOT the graph-state mapping).
    assert captured.get("call_count") == 1
    assert captured["called_with"] is interactive

    # The sentinel-derived content renders under the canonical heading.
    assert "## Relevant Prior Findings" in prompt
    assert "[fresh] port_open 10.0.0.5:22/tcp" in prompt
    assert "service=ssh" in prompt


def test_build_reflection_prompt_omits_findings_section_when_helper_returns_empty(
    monkeypatch,
) -> None:
    """An empty helper return must omit ``## Relevant Prior Findings``."""

    def fake_helper(_interactive: Any) -> list[Mapping[str, Any]]:
        return []

    monkeypatch.setattr(
        "agent.graph.nodes.reflect.build_relevant_findings_for_prompt",
        fake_helper,
    )

    interactive = InteractiveState(
        facts=FactsState(task_id=102, message="m", current_goal="g"),
        trace=TraceState(),
    )

    prompt = _build_reflection_prompt(interactive, "stuck", None)

    assert "## Relevant Prior Findings" not in prompt


def test_build_reflection_prompt_does_not_emit_legacy_findings_heading(
    monkeypatch,
) -> None:
    """The deleted parallel-path bold-line heading must never render.

    Even when the canonical helper returns matches, the prompt body
    must use the migrated ``## Relevant Prior Findings`` markdown
    heading and never the legacy ``**Relevant Prior Findings**:``
    bold-line literal that was emitted by the now-deleted
    ``_build_reflection_relevant_findings_text`` path.
    """
    sentinel = [
        {
            "kind": "service_detected",
            "target": "10.0.0.1",
            "subject": "10.0.0.1:80/tcp",
            "details": {"service": "http"},
            "assertion_level": "observed",
            "state": "fresh",
        }
    ]

    monkeypatch.setattr(
        "agent.graph.nodes.reflect.build_relevant_findings_for_prompt",
        lambda _interactive: sentinel,
    )

    interactive = InteractiveState(
        facts=FactsState(task_id=103, message="m", current_goal="g"),
        trace=TraceState(),
    )

    prompt = _build_reflection_prompt(interactive, "stuck", None)

    assert "**Relevant Prior Findings**:" not in prompt
