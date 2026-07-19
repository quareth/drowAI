"""Planner LLM and fallback generation flow.

This module owns planner generation operations: LLM client resolution,
planner LLM calls, usage accounting, clarify and scope retry calls, and
deterministic fallback plan construction. It does not mutate final graph
state, activate todos, or import the public planner node.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState
from ..emission.reasoning_section import reasoning_section
from ..utils.llm_resolver import (
    ROLE_REASONING_MAIN,
    get_llm_reasoning_effort,
    has_llm_runtime_services,
    resolve_llm_client,
)
from ..utils.scope_parser import UserScope
from ..utils.scope_validator import validate_plan_against_scope
from ..utils.todo_sync import sync_todos_with_plan
from agent.graph.config.token_limits import LIMITS
from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
    LLMProviderError,
    LLMRefusalError,
)
from core.llm import LLM_TIMEOUT_REASONING_MAIN_SEC, wait_for_with_timeout
from core.llm.structured_schemas import PLANNER_CONTRACT_STRUCTURED_OUTPUT
from . import planner_clarify, planner_prompting, planner_response
from .node_utils import append_usage_to_state
from .planner_setup import PlannerSetup

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannerLLMResolution:
    """Resolved planner LLM client and optional reasoning effort."""

    llm_client: Any
    reasoning_effort: Any


@dataclass
class PlanningGenerationResult:
    """Generated planner result or an early graph update from generation."""

    plan: List[str]
    todo_list: List[Any]
    first_goal: str
    planner_mode: str
    returned_update: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ScopeValidationRetryResult:
    """Result of deterministic scope validation and optional retry generation."""

    result: PlanningGenerationResult
    used_corrected_plan: bool


def resolve_planner_llm(
    metadata: Mapping[str, Any],
    context: Optional[GraphRuntimeContext],
    *,
    config: Optional[Dict[str, Any]] = None,
) -> PlannerLLMResolution:
    """Resolve the planner LLM client and reasoning effort."""
    try:
        llm_client = resolve_llm_client(
            metadata,
            context,
            config=config,
            role=ROLE_REASONING_MAIN,
        )
        reasoning_effort = get_llm_reasoning_effort(llm_client)
    except LLMConfigurationError:
        if has_llm_runtime_services(config):
            raise
        llm_client = None
        reasoning_effort = None
    except Exception as exc:
        logger.warning(
            "[PLANNER] Failed to resolve LLMClient, using fallback planner: %s",
            exc,
        )
        llm_client = None
        reasoning_effort = None
    return PlannerLLMResolution(
        llm_client=llm_client,
        reasoning_effort=reasoning_effort,
    )


def build_no_llm_fallback_result(
    user_message: str,
    targets: List[str],
) -> PlanningGenerationResult:
    """Build the deterministic fallback plan when no planner LLM is available."""
    logger.warning("No LLMClient available, using fallback planner")
    plan, todo_list, first_goal = planner_response.create_fallback_plan(
        user_message, targets
    )
    todo_list = sync_todos_with_plan(plan, todo_list)
    return PlanningGenerationResult(
        plan=plan,
        todo_list=todo_list,
        first_goal=first_goal,
        planner_mode="fallback",
    )


def build_existing_plan_generation_result(interactive: InteractiveState) -> Optional[PlanningGenerationResult]:
    """Return the existing plan if defensive duplicate-call protection applies."""
    facts = interactive.facts
    if facts.plan and len(facts.plan) > 0 and facts.current_goal:
        logger.warning(
            f"[PLANNER] ⚠️ LLM call blocked - plan already exists in state! "
            f"Plan has {len(facts.plan)} steps, goal={facts.current_goal[:50]}"
        )
        plan = facts.plan
        todo_list = sync_todos_with_plan(plan, facts.safe_todo_list)
        return PlanningGenerationResult(
            plan=plan,
            todo_list=todo_list,
            first_goal=facts.current_goal,
            planner_mode="existing_plan",
        )
    return None


async def call_planner_llm(
    llm_client: Any,
    *,
    system_prompt: str,
    prompt: str,
    reasoning_effort: Any,
    task_id: int,
    operation: str,
    temperature: float,
) -> Any:
    """Call the planner LLM through the timeout wrapper."""
    return await wait_for_with_timeout(
        llm_client.chat_with_usage(
            system_prompt,
            prompt,
            temperature=temperature,
            max_tokens=LIMITS.planner,
            reasoning_effort=reasoning_effort,
            structured_output=PLANNER_CONTRACT_STRUCTURED_OUTPUT,
        ),
        timeout_sec=LLM_TIMEOUT_REASONING_MAIN_SEC,
        component="REASONING_MAIN",
        operation=operation,
        logger=logger,
        task_id=task_id,
        outcome="planner_timeout",
    )


async def generate_initial_llm_plan(
    interactive: InteractiveState,
    *,
    llm_client: Any,
    reasoning_effort: Any,
    planning_prompt: str,
    env_prompt: str,
    user_message: str,
    targets: List[str],
) -> PlanningGenerationResult:
    """Generate and parse the initial planner LLM response."""
    facts = interactive.facts
    system_prompt = planner_prompting.build_planner_system_prompt(env_prompt)
    logger.info("[PLANNER] 🔵 Making LLM call to generate plan")
    llm_response = await call_planner_llm(
        llm_client,
        system_prompt=system_prompt,
        prompt=planning_prompt,
        reasoning_effort=reasoning_effort,
        task_id=facts.task_id,
        operation="planner_llm_call",
        temperature=0.3,
    )
    response = llm_response.content
    append_usage_to_state(
        interactive,
        llm_response.usage,
        "planner",
        request_mode="non_streaming",
    )
    parsed_contract = planner_response.extract_planning_contract(
        response,
        getattr(llm_response, "structured_output", None),
    )
    if parsed_contract.get("mode") == "clarify_required":
        required_blockers = planner_clarify.normalize_planner_required_blockers(parsed_contract)
        if not required_blockers:
            logger.warning(
                "[PLANNER] Invalid clarify contract received; requesting one correction"
            )
            correction_prompt = planner_prompting.build_clarify_contract_correction_prompt(
                planning_prompt
            )
            correction_response = await call_planner_llm(
                llm_client,
                system_prompt=system_prompt,
                prompt=correction_prompt,
                reasoning_effort=reasoning_effort,
                task_id=facts.task_id,
                operation="planner_clarify_correction_llm_call",
                temperature=0.2,
            )
            append_usage_to_state(
                interactive,
                correction_response.usage,
                "planner",
                request_mode="non_streaming",
            )
            response = correction_response.content
            parsed_contract = planner_response.extract_planning_contract(
                response,
                getattr(correction_response, "structured_output", None),
            )

        if parsed_contract.get("mode") == "clarify_required":
            clarify_decision = planner_clarify.apply_clarify_required_contract(
                interactive,
                parsed_contract,
            )
            if clarify_decision.is_clarify_required:
                return PlanningGenerationResult(
                    plan=[],
                    todo_list=[],
                    first_goal="",
                    planner_mode="clarify_required",
                    returned_update=clarify_decision.update or interactive.as_graph_update(),
                )

    plan, todo_list, first_goal = planner_response.parse_planning_response(
        response,
        user_message,
        targets,
        parsed_contract,
    )
    todo_list = sync_todos_with_plan(plan, todo_list)
    return PlanningGenerationResult(
        plan=plan,
        todo_list=todo_list,
        first_goal=first_goal,
        planner_mode=str(parsed_contract.get("mode") or "plan_ready"),
    )


async def retry_scope_validation_if_needed(
    interactive: InteractiveState,
    *,
    result: PlanningGenerationResult,
    llm_client: Any,
    reasoning_effort: Any,
    planning_prompt: str,
    env_prompt: str,
    user_message: str,
    targets: List[str],
    user_scope: UserScope,
) -> ScopeValidationRetryResult:
    """Validate a generated plan and run one scope correction retry if needed."""
    facts = interactive.facts
    metadata = facts.metadata
    validation = validate_plan_against_scope(result.plan, user_scope)
    if validation["valid"]:
        return ScopeValidationRetryResult(result=result, used_corrected_plan=False)

    logger.warning(
        f"[PLANNER] Plan validation failed: {validation['violations']}. "
        "Regenerating plan with scope constraints."
    )
    metadata["plan_validation"] = validation
    correction_prompt = planner_prompting.build_scope_validation_correction_prompt(
        planning_prompt,
        validation["violations"],
    )
    system_prompt = planner_prompting.build_planner_system_prompt(env_prompt)
    correction_response = await call_planner_llm(
        llm_client,
        system_prompt=system_prompt,
        prompt=correction_prompt,
        reasoning_effort=reasoning_effort,
        task_id=facts.task_id,
        operation="planner_retry_correction_llm_call",
        temperature=0.2,
    )
    append_usage_to_state(
        interactive,
        correction_response.usage,
        "planner",
        request_mode="non_streaming",
    )
    corrected_plan, corrected_todos, corrected_first_goal = planner_response.parse_planning_response(
        correction_response.content,
        user_message,
        targets,
        getattr(correction_response, "structured_output", None),
    )
    corrected_todos = sync_todos_with_plan(corrected_plan, corrected_todos)
    corrected_validation = validate_plan_against_scope(corrected_plan, user_scope)
    metadata["plan_validation_retry"] = corrected_validation
    if corrected_validation["valid"]:
        metadata["plan_validation"] = {
            "valid": True,
            "violations": [],
            "recovered_from": validation,
        }
        return ScopeValidationRetryResult(
            result=PlanningGenerationResult(
                plan=corrected_plan,
                todo_list=corrected_todos,
                first_goal=corrected_first_goal,
                planner_mode=result.planner_mode,
            ),
            used_corrected_plan=True,
        )

    logger.warning(
        "[PLANNER] Scope validation retry failed: %s",
        corrected_validation["violations"],
    )
    return ScopeValidationRetryResult(result=result, used_corrected_plan=False)


async def run_planning_generation(
    interactive: InteractiveState,
    setup: PlannerSetup,
    *,
    context: Optional[GraphRuntimeContext],
    config: Optional[Dict[str, Any]],
    writer: Any,
) -> PlanningGenerationResult:
    """Run the planner generation branch without applying final graph state."""
    metadata = setup.metadata
    targets = setup.targets
    user_message = setup.user_message
    user_scope = setup.user_scope
    env_prompt = setup.env_prompt
    planning_prompt = planner_prompting.build_planning_prompt(
        targets,
        metadata,
        available_tools=setup.available_tools,
        user_scope=user_scope,
        env_prompt=env_prompt,
    )

    llm_resolution = resolve_planner_llm(
        metadata,
        context,
        config=config,
    )
    llm_client = llm_resolution.llm_client
    reasoning_effort = llm_resolution.reasoning_effort

    if llm_client is None:
        return build_no_llm_fallback_result(
            user_message,
            targets,
        )

    planning_message = "Analyzing request and creating a plan."
    async with reasoning_section(
        writer,
        state=interactive,
        step="planning",
        label=planning_message,
        config=config,
        context=context,
    ):
        try:
            generation_result = build_existing_plan_generation_result(interactive)
            if generation_result is not None:
                return generation_result

            generation_result = await generate_initial_llm_plan(
                interactive,
                llm_client=llm_client,
                reasoning_effort=reasoning_effort,
                planning_prompt=planning_prompt,
                env_prompt=env_prompt,
                user_message=user_message,
                targets=targets,
            )
            if generation_result.returned_update is not None:
                return generation_result

            planner_clarify.mark_clarify_plan_ready(metadata)
            retry_result = await retry_scope_validation_if_needed(
                interactive,
                result=generation_result,
                llm_client=llm_client,
                reasoning_effort=reasoning_effort,
                planning_prompt=planning_prompt,
                env_prompt=env_prompt,
                user_message=user_message,
                targets=targets,
                user_scope=user_scope,
            )
            return retry_result.result

        except LLMRefusalError:
            raise
        except LLMProviderError:
            raise
        except Exception as exc:
            logger.error(f"Planning LLM call failed: {exc}")
            return build_exception_fallback_result(
                user_message,
                targets,
            )


def build_exception_fallback_result(
    user_message: str,
    targets: List[str],
) -> PlanningGenerationResult:
    """Build the deterministic fallback plan after a planner LLM exception."""
    plan, todo_list, first_goal = planner_response.create_fallback_plan(
        user_message, targets
    )
    todo_list = sync_todos_with_plan(plan, todo_list)
    return PlanningGenerationResult(
        plan=plan,
        todo_list=todo_list,
        first_goal=first_goal,
        planner_mode="fallback",
    )


__all__ = [
    "PlannerLLMResolution",
    "PlanningGenerationResult",
    "ScopeValidationRetryResult",
    "build_exception_fallback_result",
    "build_existing_plan_generation_result",
    "build_no_llm_fallback_result",
    "call_planner_llm",
    "generate_initial_llm_plan",
    "resolve_planner_llm",
    "run_planning_generation",
    "retry_scope_validation_if_needed",
]
