"""Convert synthesized tool output to compact observations for deep reasoning.

This node handles agent-facing presentation for deep reasoning loop.
It converts structured data from tool_synthesizer into compact observations
that fit in the agent's working memory without bloating token count.

This node CONTINUES the conversation (does NOT set trace.final_text).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Mapping, Optional

from agent.graph.builders.common_edges import decrement_iteration_budget
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import InteractiveState
from agent.graph.utils.goal_tracker import update_achieved_goals
from agent.graph.utils.observation_deduplication import (
    check_observation_duplicate,
    score_observation_progress,
)
from agent.graph.emission.factory import EventEmitterFactory
from agent.graph.utils.event_identity import derive_dr_stream_identifiers
from agent.graph.utils.dr_iteration_state import (
    clear_dr_active_iteration,
    record_dr_observation,
)

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

logger = logging.getLogger(__name__)


async def adapt_to_observations(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[dict] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    """
    Convert synthesized tool output to compact observations.
    
    This node:
    1. Reads structured data from metadata.synthesized_output
    2. Formats as COMPACT observation (max 1000 chars)
    3. Appends to facts.observations (NOT trace.final_text!)
    4. Continues reasoning loop
    
    Used by: Deep Reasoning flow only
    
    Memory Efficiency:
    - Nmap output: 5000 lines XML → "Found ports 22, 80, 443"
    - Only key information retained
    - Prevents state bloat across multiple iterations
    
    Args:
        state: Current interactive state
        context: Optional runtime context (unused)
    
    Returns:
        Graph update dict with new observation appended
    """
    
    interactive = InteractiveState.from_mapping(state)
    
    # Get structured data from tool_synthesizer
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    synthesized = metadata.get("synthesized_output") or {}
    articulated_observation = (synthesized.get("observation_text") or "").strip()
    # Check both legacy flag (articulated_observation_streamed) and new flag (observation_streamed)
    # for compatibility with both observation_articulation and post_tool_reasoning nodes
    already_streamed = bool(
        metadata.get("articulated_observation_streamed") or 
        metadata.get("observation_streamed")
    )
    
    # DR.6.2: Check for duplicate observations before processing
    if synthesized:
        observation_hashes = metadata.get("observation_hashes", [])
        last_observation = metadata.get("last_observation")
        
        is_duplicate, similarity, obs_hash = check_observation_duplicate(
            synthesized, observation_hashes, last_observation
        )
        
        if is_duplicate:
            # Skip processing duplicate observation
            logger.info(
                "[OBSERVATION] Skipping duplicate observation, incrementing no_progress_count"
            )
            no_progress_count = metadata.get("no_progress_count", 0) + 1
            metadata["no_progress_count"] = no_progress_count
            metadata["progress_signal"] = "no_new_findings"
            interactive.facts.metadata = metadata
            
            interactive.trace.reasoning.append(
                f"[DEDUPE] Skipped duplicate observation (similarity: {similarity:.2f})"
            )
            
            # Still decrement budget and update goals, but skip observation creation
            state_dict = interactive.as_graph_state()
            budget_update = decrement_iteration_budget(state_dict)
            if "facts" in budget_update:
                facts_update = budget_update["facts"]
                interactive.facts.iterations = facts_update.get("iterations", interactive.facts.iterations)
                if "runtime_budgets" in facts_update:
                    interactive.facts.metadata["runtime_budgets"] = facts_update["runtime_budgets"]
            
            update_achieved_goals(interactive)
            
            return interactive.as_graph_update()
        
        # DR.6.3: Score observation progress
        progress_score = score_observation_progress(synthesized, last_observation)
        metadata["last_observation_score"] = progress_score
        
        # Update no_progress_count based on progress score
        if progress_score < 0.1:
            # Very low progress (mostly duplicate)
            no_progress_count = metadata.get("no_progress_count", 0) + 1
            metadata["no_progress_count"] = no_progress_count
            metadata["progress_signal"] = "no_new_findings"
            logger.info(
                f"[OBSERVATION] Low progress score: {progress_score:.2f}, "
                f"no_progress_count: {no_progress_count}"
            )
        else:
            # Significant progress, reset counter
            metadata["no_progress_count"] = 0
            metadata["progress_signal"] = "new_findings"
            logger.debug(
                f"[OBSERVATION] Progress score: {progress_score:.2f}, reset no_progress_count"
            )
        
        # Store observation hash and last observation
        observation_hashes.append(obs_hash)
        # Limit history to last 10 to prevent unbounded growth
        if len(observation_hashes) > 10:
            observation_hashes.pop(0)
        metadata["observation_hashes"] = observation_hashes
        metadata["last_observation"] = synthesized
        interactive.facts.metadata = metadata
    
    # Check if we have synthesized data
    if not synthesized:
        # Fallback: create minimal observation
        observation = "Tool executed but no synthesis data available"
        interactive.trace.reasoning.append(
            "Observation adapter: No synthesized data found, using minimal observation"
        )
    else:
        if articulated_observation:
            observation = articulated_observation
        else:
            # Create compact observation from structured data
            observation = _create_compact_observation(synthesized)
    
    dr_iteration = None
    if (interactive.facts.capability or "").lower() == "deep_reasoning" and observation:
        # Track iteration internally (does NOT affect event identity)
        _, _, dr_iteration_val = derive_dr_stream_identifiers(
            interactive,
            config,
            advance_iteration=False,
        )
        dr_iteration = dr_iteration_val
        record_dr_observation(interactive, dr_iteration, observation)

    if writer and observation and not already_streamed:
        emitter = EventEmitterFactory.create(writer, interactive, config, context)
        try:
            emitter.emit_observation_start("observing_tool_output")
            for chunk in _chunk_text_for_stream(observation):
                emitter.emit_observation_delta(chunk)
            emitter.emit_observation_section_end("observing_tool_output")
        except Exception as stream_exc:
            logger.warning("[OBSERVATION] Streaming observation failed: %s", stream_exc)
        else:
            emitter.emit_observation_snapshot(observation, step="observing_tool_output")

    # Append to observations (NOT final_text!)
    observations = list(interactive.trace.observations or [])
    if not observations or observations[-1] != observation:
        observations.append(observation)
        interactive.trace.observations = observations
    
    if (interactive.facts.capability or "").lower() == "deep_reasoning":
        clear_dr_active_iteration(interactive)

    # DR.1 Fix: Decrement iteration budget on tool path
    # This ensures plan age expiration and iteration-based guardrails work correctly
    state_dict = interactive.as_graph_state()
    budget_update = decrement_iteration_budget(state_dict)
    
    # Apply budget updates
    if "facts" in budget_update:
        facts_update = budget_update["facts"]
        interactive.facts.iterations = facts_update.get("iterations", interactive.facts.iterations)
        if "runtime_budgets" in facts_update:
            interactive.facts.metadata["runtime_budgets"] = facts_update["runtime_budgets"]
    
    logger.debug(
        f"[OBSERVATION] Recorded observation hash and decremented iteration budget "
        f"(iteration: {interactive.facts.iterations})"
    )
    
    # DR.5.2: Update achieved goals after adapting observations
    update_achieved_goals(interactive)
    
    # Track observation creation
    interactive.trace.reasoning.append(
        f"✅ Created compact observation ({len(observation)} chars) "
        f"from {synthesized.get('tool', 'unknown')} output"
    )

    # Clear streaming flags so future runs may stream if needed
    # Both legacy flag (articulated_observation_streamed) and new flag (observation_streamed)
    # Note: Use interactive.facts.metadata directly since helper functions may have
    # replaced the metadata dict reference
    interactive.facts.metadata.pop("articulated_observation_streamed", None)
    interactive.facts.metadata.pop("observation_streamed", None)
    
    # DO NOT set trace.final_text (keep reasoning loop alive!)
    
    return interactive.as_graph_update()


def _chunk_text_for_stream(text: str, chunk_size: int = 400) -> list[str]:
    """Yield text chunks for streaming observation deltas."""
    if not text:
        return []
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def _create_compact_observation(synthesized: dict) -> str:
    """
    Create a compact observation from synthesized data.
    
    Format:
    ```
    Tool: nmap
    Findings:
      - Port 22/tcp: SSH (OpenSSH 8.2)
      - Port 80/tcp: HTTP (nginx 1.18.0)
    Vulnerabilities:
      - HTTP server version disclosure
    ```
    
    Max length: 1000 chars (hard limit to prevent memory bloat)
    
    Args:
        synthesized: Structured data from tool_synthesizer
    
    Returns:
        Compact observation string (<1000 chars)
    """
    tool_name = synthesized.get("tool", "unknown")
    status = synthesized.get("status", "unknown")
    
    obs_parts = [f"Tool: {tool_name} (status: {status})"]
    
    key_findings = synthesized.get("key_findings") or []
    if key_findings:
        obs_parts.append("Findings:")
        for finding in key_findings:
            # Truncate long findings
            truncated = finding[:100] + "..." if len(finding) > 100 else finding
            obs_parts.append(f"  - {truncated}")

    # Join and enforce hard limit
    compact_obs = "\n".join(obs_parts)

    # Hard limit: 1000 chars (prevent memory bloat)
    if len(compact_obs) > 1000:
        compact_obs = compact_obs[:997] + "..."
    
    return compact_obs


__all__ = ["adapt_to_observations"]
