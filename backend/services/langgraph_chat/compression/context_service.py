"""
Context-compression orchestration service for pass1/pass2 execution.

This module computes dynamic token thresholds from percentage policy, executes
pass1 compression first, conditionally executes pass2 corrective fallback when
pass1 output is outside the target band, and returns normalized typed outcome
metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from math import ceil, floor
from typing import Any, Awaitable, Callable, Literal, Mapping, Optional

from agent.providers.llm.core.budget_policy import decide_output_budget
from agent.providers.llm.core.identity import ProviderModelRef
from backend.config import ENABLE_CONTEXT_COMPRESSION
from backend.services.metrics.utils import safe_gauge, safe_inc
from core.llm import LLM_TIMEOUT_CONTEXT_COMPRESSOR_SEC, wait_for_with_timeout
from core.llm.role_contracts import ROLE_CONTEXT_COMPRESSOR, RoleCallSettings
from core.prompts.registry import PromptRegistry

from backend.services.langgraph_chat.compression.context_models import (
    CompressionPassResult,
    CompressionPolicy,
    CompressionRequiredError,
    ContextCompressionOutcome,
    ContextCompressionRequest,
)
from backend.services.langgraph_chat.compression.window_manager import ContextWindowManager
from backend.services.llm_provider.failure_policy import classify_llm_runtime_failure

CompressorCallable = Callable[[str, str, RoleCallSettings], Awaitable[str]]
ContextWindowManagerFactory = Callable[[int], ContextWindowManager]
CorrectionDirection = Literal["fit_band", "shorter", "longer"]
logger = logging.getLogger(__name__)
_COMPRESSOR_MAX_ATTEMPTS = 2
_COMPRESSOR_RETRY_BASE_SECONDS = 0.05
_COMPRESSOR_RETRY_JITTER_SECONDS = 0.05


@dataclass(slots=True, frozen=True)
class CompressionThresholds:
    trigger_tokens: int
    target_min_tokens: int
    target_max_tokens: int


class ContextCompressionService:
    """Run one-pass compression with pass2 fallback and typed outcome metadata."""

    def __init__(
        self,
        *,
        prompt_registry: Optional[PromptRegistry] = None,
        compressor: Optional[CompressorCallable] = None,
        context_window_manager_factory: Optional[ContextWindowManagerFactory] = None,
    ) -> None:
        self._prompt_registry = prompt_registry or PromptRegistry()
        self._compressor = compressor
        self._context_window_manager_factory = (
            context_window_manager_factory or (lambda max_tokens: ContextWindowManager(max_tokens=max_tokens))
        )

    @staticmethod
    def is_enabled() -> bool:
        """Return rollout-flag status for turn-start compression."""
        return ENABLE_CONTEXT_COMPRESSION

    @staticmethod
    def compute_thresholds(*, max_tokens: int, policy: CompressionPolicy) -> CompressionThresholds:
        """Compute dynamic trigger/target token thresholds from percentages."""
        return CompressionThresholds(
            trigger_tokens=floor(max_tokens * policy.trigger_percent / 100),
            target_min_tokens=ceil(max_tokens * policy.target_min_percent / 100),
            target_max_tokens=max(1, floor(max_tokens * policy.target_max_percent / 100)),
        )

    async def compress(
        self,
        request: ContextCompressionRequest,
        *,
        runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
        runtime_user_id: Optional[int] = None,
        purpose: str = "context_compression",
    ) -> ContextCompressionOutcome:
        """Compress context with pass1 and conditional pass2 corrective fallback."""
        thresholds = self.compute_thresholds(max_tokens=request.max_tokens, policy=request.policy)
        manager = self._context_window_manager_factory(request.max_tokens)
        original_tokens = manager.estimate_tokens_from_history(
            history=request.conversation_history,
            provider=request.provider,
            model=request.model,
            projected_user_message=request.projected_user_message,
        )
        compressor_settings = self._resolve_compressor_settings(request)

        pass1 = await self._run_pass(
            pass_name="pass1",
            system_template_id="context_compression_system_pass1",
            user_template_id="context_compression_user_pass1",
            request=request,
            compressor_settings=compressor_settings,
            token_budget_provider=request.provider,
            token_budget_model=request.model,
            target_min_tokens=thresholds.target_min_tokens,
            target_max_tokens=thresholds.target_max_tokens,
            manager=manager,
            runtime_selection=runtime_selection,
            runtime_services=runtime_services,
            runtime_user_id=runtime_user_id,
            purpose=purpose,
        )
        if pass1.within_target:
            outcome = ContextCompressionOutcome(
                request=request,
                original_tokens=original_tokens,
                final_tokens=pass1.output_tokens,
                final_text=pass1.output_text,
                pass_results=(pass1,),
                pass_count=1,
                degraded=False,
                fallback_reason=None,
            )
            safe_inc("compression_pass1_success_total")
            self._emit_compression_ratio_metric(original_tokens=outcome.original_tokens, final_tokens=outcome.final_tokens)
            return outcome

        pass2_direction = self._resolve_correction_direction(
            token_count=pass1.output_tokens,
            thresholds=thresholds,
        )
        pass2 = await self._run_pass(
            pass_name="pass2",
            system_template_id="context_compression_system_pass2",
            user_template_id="context_compression_user_pass2",
            request=request,
            compressor_settings=compressor_settings,
            token_budget_provider=request.provider,
            token_budget_model=request.model,
            target_min_tokens=thresholds.target_min_tokens,
            target_max_tokens=thresholds.target_max_tokens,
            manager=manager,
            runtime_selection=runtime_selection,
            runtime_services=runtime_services,
            runtime_user_id=runtime_user_id,
            purpose=purpose,
            previous_pass_output=pass1.output_text,
            previous_pass_tokens=pass1.output_tokens,
            correction_direction=pass2_direction,
        )
        if pass2.within_target:
            fallback_reason = "pass1_above_target" if pass2_direction == "shorter" else "pass1_below_target"
            outcome = ContextCompressionOutcome(
                request=request,
                original_tokens=original_tokens,
                final_tokens=pass2.output_tokens,
                final_text=pass2.output_text,
                pass_results=(pass1, pass2),
                pass_count=2,
                degraded=False,
                fallback_reason=fallback_reason,
            )
            safe_inc("compression_pass2_used_total")
            self._emit_compression_ratio_metric(original_tokens=outcome.original_tokens, final_tokens=outcome.final_tokens)
            return outcome

        degraded_text, degraded_tokens = self._build_degraded_fallback(
            source_text=pass2.output_text,
            source_history=request.conversation_history,
            target_min_tokens=thresholds.target_min_tokens,
            target_max_tokens=thresholds.target_max_tokens,
            manager=manager,
            provider=request.provider,
            model=request.model,
        )
        degraded_within_target = thresholds.target_min_tokens <= degraded_tokens <= thresholds.target_max_tokens
        fallback_reason = "pass2_above_target_degraded" if pass2_direction == "shorter" else "pass2_below_target_degraded"
        degraded_pass = CompressionPassResult(
            pass_name="degraded",
            system_template_id=pass2.system_template_id,
            user_template_id=pass2.user_template_id,
            output_text=degraded_text,
            output_tokens=degraded_tokens,
            target_max_tokens=thresholds.target_max_tokens,
            within_target=degraded_within_target,
        )
        outcome = ContextCompressionOutcome(
            request=request,
            original_tokens=original_tokens,
            final_tokens=degraded_pass.output_tokens,
            final_text=degraded_pass.output_text,
            pass_results=(pass1, pass2, degraded_pass),
            pass_count=3,
            degraded=True,
            fallback_reason=fallback_reason,
        )
        safe_inc("compression_pass2_used_total")
        safe_inc("compression_degraded_total")
        self._emit_compression_ratio_metric(original_tokens=outcome.original_tokens, final_tokens=outcome.final_tokens)
        return outcome

    @staticmethod
    def _resolve_correction_direction(
        *,
        token_count: int,
        thresholds: CompressionThresholds,
    ) -> CorrectionDirection:
        if token_count > thresholds.target_max_tokens:
            return "shorter"
        if token_count < thresholds.target_min_tokens:
            return "longer"
        return "fit_band"

    async def _run_pass(
        self,
        *,
        pass_name: str,
        system_template_id: str,
        user_template_id: str,
        request: ContextCompressionRequest,
        compressor_settings: RoleCallSettings,
        token_budget_provider: str,
        token_budget_model: str,
        target_min_tokens: int,
        target_max_tokens: int,
        manager: ContextWindowManager,
        runtime_selection: Optional[Mapping[str, Any]],
        runtime_services: Any,
        runtime_user_id: Optional[int],
        purpose: str,
        previous_pass_output: Optional[str] = None,
        previous_pass_tokens: Optional[int] = None,
        correction_direction: CorrectionDirection = "fit_band",
    ) -> CompressionPassResult:
        system_prompt = self._prompt_registry.get_template(system_template_id)
        user_prompt = self._render_user_prompt(
            user_template_id=user_template_id,
            request=request,
            previous_pass_output=previous_pass_output,
            target_min_tokens=target_min_tokens,
            target_max_tokens=target_max_tokens,
            previous_pass_tokens=previous_pass_tokens,
            correction_direction=correction_direction,
        )
        max_output_tokens = self._validate_compressor_request_fit(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            compressor_settings=compressor_settings,
            target_max_tokens=target_max_tokens,
            manager=manager,
        )
        output_text = await self._invoke_compressor(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            call_settings=compressor_settings,
            max_tokens=max_output_tokens,
            runtime_selection=runtime_selection,
            runtime_services=runtime_services,
            runtime_user_id=runtime_user_id,
            task_id=request.task_id,
            purpose=purpose,
        )
        output_tokens = self._estimate_assistant_tokens(
            manager=manager,
            text=output_text,
            provider=token_budget_provider,
            model=token_budget_model,
        )
        return CompressionPassResult(
            pass_name=pass_name,  # type: ignore[arg-type]
            system_template_id=system_template_id,
            user_template_id=user_template_id,
            output_text=output_text,
            output_tokens=output_tokens,
            target_max_tokens=target_max_tokens,
            within_target=target_min_tokens <= output_tokens <= target_max_tokens,
        )

    async def _invoke_compressor(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        call_settings: RoleCallSettings,
        max_tokens: Optional[int] = None,
        runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
        runtime_user_id: Optional[int] = None,
        task_id: Optional[int] = None,
        purpose: str = "context_compression",
    ) -> str:
        for attempt in range(1, _COMPRESSOR_MAX_ATTEMPTS + 1):
            try:
                return await self._invoke_compressor_once(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    call_settings=call_settings,
                    max_tokens=max_tokens,
                    runtime_selection=runtime_selection,
                    runtime_services=runtime_services,
                    runtime_user_id=runtime_user_id,
                    task_id=task_id,
                    purpose=purpose,
                )
            except Exception as exc:
                disposition = classify_llm_runtime_failure(exc)
                retryable = disposition.retryable and disposition.kind in {
                    "timeout",
                    "provider_api",
                }
                if not retryable or attempt >= _COMPRESSOR_MAX_ATTEMPTS:
                    raise
                delay_seconds = min(
                    _COMPRESSOR_RETRY_BASE_SECONDS
                    + random.uniform(0.0, _COMPRESSOR_RETRY_JITTER_SECONDS),
                    _COMPRESSOR_RETRY_BASE_SECONDS
                    + _COMPRESSOR_RETRY_JITTER_SECONDS,
                )
                logger.warning(
                    "context_compaction.retry task_id=%s attempt=%s failure_kind=%s",
                    task_id,
                    attempt + 1,
                    disposition.kind,
                )
                await asyncio.sleep(delay_seconds)
        raise RuntimeError("unreachable compressor retry state")

    async def _invoke_compressor_once(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        call_settings: RoleCallSettings,
        max_tokens: Optional[int] = None,
        runtime_selection: Optional[Mapping[str, Any]] = None,
        runtime_services: Any = None,
        runtime_user_id: Optional[int] = None,
        task_id: Optional[int] = None,
        purpose: str = "context_compression",
    ) -> str:
        """Send one compressor provider request without compatibility resubmission."""
        if self._compressor is not None:
            return await self._compressor(system_prompt, user_prompt, call_settings)
        resolver = getattr(runtime_services, "client_resolver", None)
        if resolver is not None and isinstance(runtime_selection, Mapping) and runtime_user_id is not None:
            client = resolver.get_client(
                runtime_selection,
                target=call_settings,
                runtime_user_id=runtime_user_id,
                task_id=task_id,
                purpose=purpose,
                resolution_role=ROLE_CONTEXT_COMPRESSOR,
                resolution_source=call_settings.source,
            )
            chat_kwargs: dict[str, Any] = {"temperature": 0.0}
            if max_tokens is not None:
                chat_kwargs["max_tokens"] = max_tokens
            return await wait_for_with_timeout(
                client.chat(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    **chat_kwargs,
                ),
                timeout_sec=LLM_TIMEOUT_CONTEXT_COMPRESSOR_SEC,
                component="CONTEXT_COMPRESSOR",
                operation="context_compression_llm_call",
                logger=logger,
                task_id=task_id,
                outcome="compression_timeout",
            )
        raise ValueError(
            "context compression requires runtime_services.client_resolver, "
            "runtime_selection, and runtime_user_id"
        )

    @staticmethod
    def _resolve_compressor_settings(
        request: ContextCompressionRequest,
    ) -> RoleCallSettings:
        """Use the task-selected provider/model as the compressor target."""
        selected_ref = ProviderModelRef(
            request.provider,
            request.model,
        ).normalized()
        return RoleCallSettings(
            provider=selected_ref.provider,
            model=selected_ref.model,
            reasoning_effort=None,
            source="user_selected",
        )

    @staticmethod
    def _validate_compressor_request_fit(
        *,
        system_prompt: str,
        user_prompt: str,
        compressor_settings: RoleCallSettings,
        target_max_tokens: int,
        manager: ContextWindowManager,
    ) -> int:
        """Fail before provider send when the rendered request cannot fit."""
        prompt_tokens = manager.estimate_tokens_from_history(
            history=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            provider=compressor_settings.provider,
            model=compressor_settings.model,
        )
        budget = decide_output_budget(
            provider=compressor_settings.provider,
            model=compressor_settings.model,
            role=ROLE_CONTEXT_COMPRESSOR,
            requested_max_output_tokens=target_max_tokens,
            context_estimate_tokens=prompt_tokens,
        )
        if budget.should_fail or budget.accepted_max_tokens is None:
            overflow_tokens = (
                budget.context_fit.overflow_tokens
                if budget.context_fit is not None
                else 0
            )
            raise CompressionRequiredError(
                reason="compressor_request_exceeds_context",
                detail=(
                    f"provider={budget.provider} model={budget.model} "
                    f"prompt_tokens={prompt_tokens} "
                    f"requested_output_tokens={target_max_tokens} "
                    f"context_limit_tokens={budget.context_window_tokens} "
                    f"overflow_tokens={overflow_tokens}"
                ),
            )
        return budget.accepted_max_tokens

    def _render_user_prompt(
        self,
        *,
        user_template_id: str,
        request: ContextCompressionRequest,
        previous_pass_output: Optional[str] = None,
        target_min_tokens: int,
        target_max_tokens: int,
        previous_pass_tokens: Optional[int] = None,
        correction_direction: CorrectionDirection = "fit_band",
    ) -> str:
        template = self._prompt_registry.get_template(user_template_id)
        return template.format(
            conversation_history=json.dumps(request.conversation_history, ensure_ascii=True),
            projected_user_message=request.projected_user_message or "",
            previous_pass_output=previous_pass_output or "",
            previous_pass_tokens=max(0, int(previous_pass_tokens or 0)),
            target_min_tokens=target_min_tokens,
            target_max_tokens=target_max_tokens,
            target_range_tokens=f"{target_min_tokens}-{target_max_tokens}",
            budget_direction=correction_direction,
        )

    def _build_degraded_fallback(
        self,
        *,
        source_text: str,
        source_history: list[dict[str, Any]],
        target_min_tokens: int,
        target_max_tokens: int,
        manager: ContextWindowManager,
        provider: str,
        model: str,
    ) -> tuple[str, int]:
        lines = [line.strip() for line in source_text.splitlines() if line.strip()]
        if lines:
            candidate = "\n".join(lines[:3])
        else:
            candidate = (
                "Core facts: preserve latest known facts.\n"
                "Pending actions: continue from latest user request.\n"
                "Constraints and safety: preserve prior safety constraints."
            )
        source_context_text = self._history_as_continuity_text(source_history)
        return self._fit_to_token_band(
            text=candidate,
            source_context_text=source_context_text,
            target_min_tokens=target_min_tokens,
            target_max_tokens=target_max_tokens,
            manager=manager,
            provider=provider,
            model=model,
        )

    def _fit_to_token_band(
        self,
        *,
        text: str,
        source_context_text: str,
        target_min_tokens: int,
        target_max_tokens: int,
        manager: ContextWindowManager,
        provider: str,
        model: str,
    ) -> tuple[str, int]:
        candidate = text.strip() or "Context compressed."
        candidate, tokens = self._trim_to_max_tokens(
            text=candidate,
            target_max_tokens=target_max_tokens,
            manager=manager,
            provider=provider,
            model=model,
        )
        if tokens >= target_min_tokens:
            return candidate, tokens

        source = source_context_text.strip()
        if not source:
            return candidate, tokens

        merged = f"{candidate}\n\n{source}" if candidate else source
        merged_candidate, merged_tokens = self._trim_to_max_tokens(
            text=merged,
            target_max_tokens=target_max_tokens,
            manager=manager,
            provider=provider,
            model=model,
        )
        if merged_tokens >= target_min_tokens:
            return merged_candidate, merged_tokens

        source_candidate, source_tokens = self._trim_to_max_tokens(
            text=source,
            target_max_tokens=target_max_tokens,
            manager=manager,
            provider=provider,
            model=model,
        )
        if source_tokens > merged_tokens:
            return source_candidate, source_tokens
        return merged_candidate, merged_tokens

    def _trim_to_max_tokens(
        self,
        *,
        text: str,
        target_max_tokens: int,
        manager: ContextWindowManager,
        provider: str,
        model: str,
    ) -> tuple[str, int]:
        candidate = text.strip() or "C"
        tokens = self._estimate_assistant_tokens(
            manager=manager,
            text=candidate,
            provider=provider,
            model=model,
        )
        if tokens <= target_max_tokens:
            return candidate, tokens

        best_candidate = "C"
        best_tokens = self._estimate_assistant_tokens(
            manager=manager,
            text=best_candidate,
            provider=provider,
            model=model,
        )
        low = 1
        high = len(candidate)
        while low <= high:
            middle = (low + high) // 2
            trial_candidate = candidate[:middle].rstrip() or "C"
            trial_tokens = self._estimate_assistant_tokens(
                manager=manager,
                text=trial_candidate,
                provider=provider,
                model=model,
            )
            if trial_tokens <= target_max_tokens:
                best_candidate = trial_candidate
                best_tokens = trial_tokens
                low = middle + 1
            else:
                high = middle - 1
        return best_candidate, best_tokens

    @staticmethod
    def _estimate_assistant_tokens(
        *,
        manager: ContextWindowManager,
        text: str,
        provider: str,
        model: str,
    ) -> int:
        return manager.estimate_tokens_from_history(
            history=[{"role": "assistant", "content": text}],
            provider=provider,
            model=model,
        )

    @classmethod
    def _history_as_continuity_text(cls, history: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for item in history:
            role_raw = item.get("role")
            role = str(role_raw).strip() if isinstance(role_raw, str) else "unknown"
            if not role:
                role = "unknown"
            content = cls._normalize_message_content(item.get("content"))
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines).strip()

    @staticmethod
    def _normalize_message_content(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()
        try:
            return json.dumps(content, ensure_ascii=True)
        except TypeError:
            return str(content).strip()

    @staticmethod
    def _emit_compression_ratio_metric(*, original_tokens: int, final_tokens: int) -> None:
        if original_tokens <= 0:
            return
        safe_gauge("compression_ratio_before_after", float(final_tokens) / float(original_tokens))


__all__ = ["CompressionThresholds", "ContextCompressionService"]
