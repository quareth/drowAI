"""Capability-specific guardrails for PTR output.

Historically this module enforced a strict single-step policy for the
``simple_tool_execution`` (LLM-facing ``direct_executor``) branch: any
non-recovery follow-up ``call_tool`` decision was coerced to ``finalize``.

That enforcement is now owned by
``policies/direct_executor.apply_direct_executor_policy``, which expresses
bounded continuation rules (goal achieved, budget exhausted, repeated
no-progress) using existing runtime state instead of a blanket single-step
rule. See the composed policy order documented at the PTR node orchestration
site (``node.py``).

The function below is retained as a narrowed no-op so the symbol remains
importable and so a future capability-specific guardrail can be added here
without re-plumbing the call site. It intentionally does nothing for
``simple_tool_execution`` (direct-executor policy is authoritative) and has
no behavior for any other capability today.
"""

from __future__ import annotations

import logging

from ..models import PostToolReasoningOutput

logger = logging.getLogger(__name__)


def _enforce_simple_tool_single_step_policy(
    output: PostToolReasoningOutput,
    capability: str,
) -> None:
    """No-op shim for the former simple-tool single-step guardrail.

    The bounded continuation contract for ``simple_tool_execution`` is now
    enforced by
    ``policies.direct_executor.apply_direct_executor_policy``. This function
    is intentionally kept as an import-stable no-op; it does NOT coerce
    ``call_tool`` to ``finalize`` for ``simple_tool_execution`` anymore,
    since that contradicts the new bounded-continuation contract where
    lightweight multi-step workflows are explicitly allowed within budget.

    For any other capability this has never had behavior and still does not.
    """
    # Explicitly ignored; kept only to document the new ownership boundary.
    _ = output
    _ = capability
    return

