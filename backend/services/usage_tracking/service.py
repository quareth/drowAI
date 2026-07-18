"""Usage Tracking Service for recording and querying token usage.

This service provides the central interface for:
- Recording token usage from LLM API calls
- Querying aggregated usage per task/user
- Cost calculation using stored token counts
- Optional caching with auto-invalidation

Design decisions:
- Async operations for non-blocking DB access
- Non-critical: failures are logged but don't disrupt main request flow
- Per-request granularity for debugging and per-node tracking
- Optional cache integration with automatic invalidation on new records
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.llm import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    LLMUsageRecord,
)

from .insights_models import UsageRecordMetadata, serialize_usage_metadata
from .models import TaskUsageSummary, UsageData
from .pricing import (
    PRICING_UNAVAILABLE,
    aggregate_pricing_statuses,
    calculate_cost,
    pricing_status_for_usage,
    usage_from_persisted_record,
)

if TYPE_CHECKING:
    from .cache import UsageCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ProviderModelUsageRow:
    """Normalized provider/model token aggregate for task summary costing."""

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    reasoning_tokens: int


class UsageTrackingService:
    """Central service for recording and querying token usage.
    
    This service handles all database operations for token tracking.
    It uses synchronous sessions for compatibility with the existing
    backend infrastructure.
    
    Optionally accepts a cache instance for automatic invalidation
    when new usage records are created.
    
    Usage:
        # Basic usage without caching
        service = UsageTrackingService(db)
        
        # With caching (auto-invalidates on new records)
        cache = UsageCache(ttl_seconds=30)
        service = UsageTrackingService(db, cache=cache)
        
        # Record usage (invalidates cache if provided)
        record = service.record_usage(
            task_id=123,
            user_id=1,
            usage=usage_data,
            source="langgraph_normal",
        )
        
        # Query aggregated usage (cache-aware)
        summary = service.get_task_usage(task_id=123)
        print(f"Total tokens: {summary.total_tokens}")
        print(f"Cost: ${summary.total_cost_usd:.4f}")
    """
    
    def __init__(self, db: Session, cache: Optional["UsageCache"] = None):
        """Initialize service with database session and optional cache.
        
        Args:
            db: SQLAlchemy session for database operations
            cache: Optional UsageCache for caching summaries with auto-invalidation
        """
        self._db = db
        self._cache = cache
    
    def record_usage(
        self,
        task_id: int,
        user_id: int,
        usage: UsageData,
        source: str,
        conversation_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        usage_metadata: Optional[UsageRecordMetadata] = None,
        connection_id: Optional[str] = None,
        deployment_id: Optional[str] = None,
        route_id: Optional[str] = None,
    ) -> Optional[LLMUsageRecord]:
        """Record a single LLM call's usage.

        This is designed for concurrent calls and handles failures gracefully.
        Failures are logged but do not propagate to avoid disrupting
        the main request flow.

        Args:
            task_id: ID of the task this usage belongs to
            user_id: ID of the user who initiated the request
            usage: UsageData instance with token counts
            source: Identifier for the code path (e.g., "langgraph_normal").
                Retained as a coarse routing/debug field; insights grouping
                reads ``usage_metadata.role`` / ``node_name`` instead.
            conversation_id: Optional conversation ID for multi-turn tracking
            metadata: Legacy free-form debug dict. Kept for backward
                compatibility with callers that do not yet produce a
                canonical ``UsageRecordMetadata``. Ignored when
                ``usage_metadata`` is provided so the canonical contract
                always wins.
            usage_metadata: Canonical ``UsageRecordMetadata`` for this call.
                Serialized via ``serialize_usage_metadata`` into the
                ``LLMUsageRecord.request_metadata`` JSON column so the
                insights read layer groups on stable keys without parsing
                ``source`` strings. When omitted, the record is written
                without structured metadata (legacy behavior) or with the
                legacy ``metadata`` dict if provided.
            connection_id: Optional deployment-aware connection identity for
                this call. When omitted and ``deployment_id`` is present, the
                connection is resolved from the deployment row.
            deployment_id: Optional deployment identity for this call.
            route_id: Optional route identity for this call. Ignored unless it
                belongs to ``deployment_id``.

        Returns:
            Created LLMUsageRecord if successful, None on failure
        """
        if usage.is_empty():
            logger.debug(f"Skipping empty usage record for task {task_id}")
            return None

        tenant_id = self._resolve_tenant_id(task_id=task_id, user_id=user_id)
        (
            resolved_connection_id,
            resolved_deployment_id,
            resolved_route_id,
        ) = self._resolve_usage_identity(
            user_id=user_id,
            connection_id=connection_id,
            deployment_id=deployment_id,
            route_id=route_id,
        )

        # Canonical metadata wins when provided; legacy ``metadata`` dict is
        # preserved as a fallback for callers that still pass debug-only info.
        if usage_metadata is not None:
            request_metadata_payload: Optional[Dict[str, Any]] = serialize_usage_metadata(
                usage_metadata
            )
        else:
            request_metadata_payload = (
                dict(metadata) if isinstance(metadata, dict) else metadata
            )

        if isinstance(request_metadata_payload, dict):
            request_metadata_payload = dict(request_metadata_payload)
            _fill_missing_metadata_value(
                request_metadata_payload,
                "provider",
                str(usage.provider or "").strip().lower(),
            )
            _fill_missing_metadata_value(
                request_metadata_payload,
                "api_surface",
                str(usage.api_surface or "").strip().lower(),
            )
            _fill_missing_metadata_value(
                request_metadata_payload,
                "cache_reporting",
                str(usage.cache_reporting or "").strip().lower(),
            )

        if usage.provider_usage_components is not None:
            components_payload = usage.provider_usage_components.to_dict()
            if request_metadata_payload is None:
                request_metadata_payload = {}
            elif isinstance(request_metadata_payload, dict):
                request_metadata_payload = dict(request_metadata_payload)
            else:
                request_metadata_payload = {"legacy_metadata": request_metadata_payload}
            request_metadata_payload["provider_usage_components"] = components_payload
            _fill_missing_metadata_value(
                request_metadata_payload,
                "provider",
                str(usage.provider or "").strip().lower(),
            )
            _fill_missing_metadata_value(
                request_metadata_payload,
                "api_surface",
                str(usage.api_surface or "").strip().lower(),
            )

        try:
            record = LLMUsageRecord(
                task_id=task_id,
                tenant_id=tenant_id,
                user_id=user_id,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cached_tokens=usage.cached_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                model=usage.model,
                provider=usage.provider,
                connection_id=resolved_connection_id,
                deployment_id=resolved_deployment_id,
                route_id=resolved_route_id,
                source=source,
                conversation_id=conversation_id,
                request_metadata=request_metadata_payload,
            )
            self._db.add(record)
            self._db.commit()
            self._db.refresh(record)
            
            # Auto-invalidate cache if provided
            if self._cache is not None:
                self._cache.invalidate(task_id)
                logger.debug(f"Cache invalidated for task {task_id}")
            
            logger.debug(
                f"Recorded usage for task {task_id}: "
                f"{usage.prompt_tokens}+{usage.completion_tokens}={usage.total_tokens} tokens "
                f"(source={source}, model={usage.model})"
            )
            return record
            
        except Exception as exc:
            # Log but don't fail the request - usage tracking is non-critical
            logger.warning(
                f"Failed to record usage for task {task_id}: {exc}",
                exc_info=True,
            )
            try:
                self._db.rollback()
            except Exception:
                pass
            return None

    def _resolve_usage_identity(
        self,
        *,
        user_id: int,
        connection_id: Optional[str],
        deployment_id: Optional[str],
        route_id: Optional[str],
    ) -> tuple[object | None, object | None, object | None]:
        """Validate optional deployment refs before writing a usage row."""

        resolved_connection_id: object | None = None
        resolved_deployment_id: object | None = None
        resolved_route_id: object | None = None

        if deployment_id:
            deployment = self._db.get(LLMModelDeployment, deployment_id)
            if deployment is None:
                return None, None, None
            connection = self._db.get(LLMInferenceConnection, deployment.connection_id)
            if connection is None or int(connection.user_id) != int(user_id):
                return None, None, None
            resolved_connection_id = connection.id
            resolved_deployment_id = deployment.id
            if route_id:
                route = self._db.get(LLMDeploymentRoute, route_id)
                if route is not None and route.deployment_id == deployment.id:
                    resolved_route_id = route.id
            return resolved_connection_id, resolved_deployment_id, resolved_route_id

        if connection_id:
            connection = self._db.get(LLMInferenceConnection, connection_id)
            if connection is not None and int(connection.user_id) == int(user_id):
                resolved_connection_id = connection.id

        return resolved_connection_id, resolved_deployment_id, resolved_route_id

    def _resolve_tenant_id(self, *, task_id: int, user_id: int) -> int:
        """Resolve usage ownership from the authoritative task tenant."""

        task_tenant_id = self._db.execute(
            select(Task.tenant_id).where(Task.id == task_id)
        ).scalar_one_or_none()
        if task_tenant_id is not None:
            return int(task_tenant_id)

        raise ValueError(f"Cannot resolve tenant for usage row without task ownership: task_id={task_id}")
    
    def get_task_usage(
        self,
        task_id: int,
        *,
        tenant_id: int | None = None,
        use_cache: bool = True,
    ) -> TaskUsageSummary:
        """Get aggregated usage for a task.
        
        Performs a single efficient query to aggregate all usage records
        for the specified task, then calculates the total cost based
        on the tokens used per model.
        
        If a cache is configured and use_cache=True, checks cache first
        and populates it on cache miss.
        
        Args:
            task_id: ID of the task to get usage for
            use_cache: Whether to use caching (default: True)
            
        Returns:
            TaskUsageSummary with aggregated token counts and cost
        """
        use_cache = bool(use_cache and tenant_id is None)

        # Check cache first if available
        if use_cache and self._cache is not None:
            cached = self._cache.get(task_id)
            if cached is not None:
                logger.debug(f"Cache hit for task {task_id}")
                return cached
        
        try:
            # Get aggregated totals in single query
            result = self._db.execute(
                select(
                    func.coalesce(func.sum(LLMUsageRecord.prompt_tokens), 0),
                    func.coalesce(func.sum(LLMUsageRecord.completion_tokens), 0),
                    func.coalesce(func.sum(LLMUsageRecord.total_tokens), 0),
                    func.coalesce(func.sum(LLMUsageRecord.cached_tokens), 0),
                    func.coalesce(func.sum(LLMUsageRecord.reasoning_tokens), 0),
                    func.count(LLMUsageRecord.id),
                    func.min(LLMUsageRecord.created_at),
                    func.max(LLMUsageRecord.created_at),
                )
                .where(
                    LLMUsageRecord.task_id == task_id,
                    self._tenant_filter(tenant_id),
                )
            ).one()
            
            prompt_tokens = int(result[0])
            completion_tokens = int(result[1])
            total_tokens = int(result[2])
            cached_tokens = int(result[3])
            reasoning_tokens = int(result[4])
            call_count = int(result[5])
            first_call = result[6]
            last_call = result[7]
            
            provider_model_rows = self._get_provider_model_usage_rows(task_id, tenant_id=tenant_id)
            pricing_records = self._get_task_usage_records(task_id, tenant_id=tenant_id)
            models_used = sorted(
                {
                    row.model
                    for row in provider_model_rows
                    if row.model
                }
            )
            if pricing_records:
                total_cost = self._calculate_cost_from_records(pricing_records)
                record_pricing = [
                    (
                        record,
                        pricing_status_for_usage(usage_from_persisted_record(record)),
                    )
                    for record in pricing_records
                ]
                row_pricing_statuses = [status for _record, status in record_pricing]
                unpriced_providers = sorted(
                    {
                        str(record.provider or "openai").strip().lower() or "openai"
                        for record, status in record_pricing
                        if status == PRICING_UNAVAILABLE
                    }
                )
                unpriced_models = sorted(
                    {
                        _provider_model_label(
                            str(record.provider or "openai").strip().lower() or "openai",
                            str(record.model or "unknown"),
                        )
                        for record, status in record_pricing
                        if status == PRICING_UNAVAILABLE
                    }
                )
            else:
                total_cost = self._calculate_cost_from_provider_model_rows(provider_model_rows)
                aggregate_pricing = [
                    (
                        row,
                        pricing_status_for_usage(self._usage_from_provider_model_row(row)),
                    )
                    for row in provider_model_rows
                ]
                row_pricing_statuses = [status for _row, status in aggregate_pricing]
                unpriced_providers = sorted(
                    {
                        row.provider
                        for row, status in aggregate_pricing
                        if status == PRICING_UNAVAILABLE
                    }
                )
                unpriced_models = sorted(
                    {
                        _provider_model_label(row.provider, row.model)
                        for row, status in aggregate_pricing
                        if status == PRICING_UNAVAILABLE
                    }
                )
            pricing_status = aggregate_pricing_statuses(row_pricing_statuses)
            
            summary = TaskUsageSummary(
                task_id=task_id,
                total_prompt_tokens=prompt_tokens,
                total_completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                total_cached_tokens=cached_tokens,
                total_reasoning_tokens=reasoning_tokens,
                total_cost_usd=total_cost,
                call_count=call_count,
                models_used=models_used,
                pricing_status=pricing_status,
                unpriced_providers=unpriced_providers,
                unpriced_models=unpriced_models,
                first_call=first_call,
                last_call=last_call,
            )
            
            # Populate cache if available
            if use_cache and self._cache is not None:
                self._cache.set(task_id, summary)
                logger.debug(f"Cache populated for task {task_id}")
            
            return summary
            
        except Exception as exc:
            logger.warning(
                f"Failed to get task usage for task {task_id}: {exc}",
                exc_info=True,
            )
            return TaskUsageSummary.empty(task_id)

    def _get_provider_model_usage_rows(
        self,
        task_id: int,
        *,
        tenant_id: int | None = None,
    ) -> List[_ProviderModelUsageRow]:
        """Return normalized provider/model token aggregates for one task."""

        rows = self._db.execute(
            select(
                LLMUsageRecord.provider,
                LLMUsageRecord.model,
                func.sum(LLMUsageRecord.prompt_tokens),
                func.sum(LLMUsageRecord.completion_tokens),
                func.sum(LLMUsageRecord.cached_tokens),
                func.sum(LLMUsageRecord.reasoning_tokens),
            )
            .where(
                LLMUsageRecord.task_id == task_id,
                self._tenant_filter(tenant_id),
            )
            .group_by(LLMUsageRecord.provider, LLMUsageRecord.model)
        ).all()
        return [self._normalize_provider_model_usage_row(row) for row in rows]

    def _get_task_usage_records(
        self,
        task_id: int,
        *,
        tenant_id: int | None = None,
    ) -> List[LLMUsageRecord]:
        """Return persisted rows needed for quote-level pricing decisions."""

        rows = self._db.execute(
            select(LLMUsageRecord).where(
                LLMUsageRecord.task_id == task_id,
                self._tenant_filter(tenant_id),
            )
        ).scalars().all()
        return list(rows)

    @staticmethod
    def _normalize_provider_model_usage_row(row: Any) -> _ProviderModelUsageRow:
        """Normalize a DB or mock aggregate row into provider-aware fields."""

        provider, model, prompt, completion, cached, reasoning = row
        normalized_provider = str(provider or "openai").strip().lower() or "openai"
        return _ProviderModelUsageRow(
            provider=normalized_provider,
            model=str(model or "unknown"),
            prompt_tokens=int(prompt or 0),
            completion_tokens=int(completion or 0),
            cached_tokens=int(cached or 0),
            reasoning_tokens=int(reasoning or 0),
        )

    def _calculate_cost_from_provider_model_rows(
        self,
        rows: List[_ProviderModelUsageRow],
    ) -> float:
        """Calculate cost from provider/model aggregates without losing provider identity."""

        total_cost = 0.0
        for row in rows:
            total_cost += calculate_cost(self._usage_from_provider_model_row(row))
        return total_cost

    @staticmethod
    def _usage_from_provider_model_row(row: _ProviderModelUsageRow) -> UsageData:
        """Build normalized usage from a provider/model aggregate row."""

        return UsageData(
            prompt_tokens=row.prompt_tokens,
            completion_tokens=row.completion_tokens,
            total_tokens=row.prompt_tokens + row.completion_tokens,
            model=row.model,
            provider=row.provider,
            cached_tokens=row.cached_tokens,
            reasoning_tokens=row.reasoning_tokens,
        )

    def _calculate_cost_from_records(self, records: List[LLMUsageRecord]) -> float:
        """Calculate task cost by summing quote-level per-record costs."""

        total_cost = 0.0
        for record in records:
            total_cost += calculate_cost(usage_from_persisted_record(record))
        return total_cost
    
    def _calculate_task_cost(self, task_id: int) -> float:
        """Calculate total cost by summing per-record costs.
        
        This ensures accurate pricing when multiple models are used
        in a single task.
        """
        try:
            return self._calculate_cost_from_provider_model_rows(
                self._get_provider_model_usage_rows(task_id)
            )
            
        except Exception as exc:
            logger.warning(f"Failed to calculate task cost: {exc}")
            return 0.0
    
    def get_task_usage_breakdown(
        self, 
        task_id: int,
        *,
        tenant_id: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[LLMUsageRecord]:
        """Get per-call breakdown for a task.
        
        Returns individual usage records for debugging and detailed analysis.
        
        Args:
            task_id: ID of the task to get breakdown for
            limit: Maximum number of records to return
            offset: Number of records to skip (for pagination)
            
        Returns:
            List of LLMUsageRecord objects
        """
        try:
            records = self._db.execute(
                select(LLMUsageRecord)
                .where(
                    LLMUsageRecord.task_id == task_id,
                    self._tenant_filter(tenant_id),
                )
                .order_by(LLMUsageRecord.created_at.desc())
                .limit(limit)
                .offset(offset)
            ).scalars().all()
            
            return list(records)
            
        except Exception as exc:
            logger.warning(
                f"Failed to get task usage breakdown for task {task_id}: {exc}",
                exc_info=True,
            )
            return []

    def get_tenant_usage_export(self, *, tenant_id: int) -> Dict[str, Any]:
        """Return tenant-wide usage export summary across all tenant tasks."""

        scoped_tenant_id = int(tenant_id)
        result = self._db.execute(
            select(
                func.count(func.distinct(LLMUsageRecord.task_id)),
                func.count(LLMUsageRecord.id),
                func.coalesce(func.sum(LLMUsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsageRecord.completion_tokens), 0),
                func.coalesce(func.sum(LLMUsageRecord.total_tokens), 0),
                func.coalesce(func.sum(LLMUsageRecord.cached_tokens), 0),
                func.coalesce(func.sum(LLMUsageRecord.reasoning_tokens), 0),
            ).where(LLMUsageRecord.tenant_id == scoped_tenant_id)
        ).one()

        task_count = int(result[0] or 0)
        call_count = int(result[1] or 0)
        prompt_tokens = int(result[2] or 0)
        completion_tokens = int(result[3] or 0)
        total_tokens = int(result[4] or 0)
        cached_tokens = int(result[5] or 0)
        reasoning_tokens = int(result[6] or 0)

        records = self._db.execute(
            select(LLMUsageRecord).where(LLMUsageRecord.tenant_id == scoped_tenant_id)
        ).scalars().all()
        normalized_records = list(records)
        models = sorted({str(row.model or "unknown") for row in normalized_records})
        total_cost = self._calculate_cost_from_records(normalized_records)
        record_pricing = [
            (row, pricing_status_for_usage(usage_from_persisted_record(row)))
            for row in normalized_records
        ]
        pricing_status = aggregate_pricing_statuses([status for _row, status in record_pricing])
        unpriced_providers = sorted(
            {
                str(row.provider or "openai").strip().lower() or "openai"
                for row, status in record_pricing
                if status == PRICING_UNAVAILABLE
            }
        )
        unpriced_models = sorted(
            {
                _provider_model_label(
                    str(row.provider or "openai").strip().lower() or "openai",
                    str(row.model or "unknown"),
                )
                for row, status in record_pricing
                if status == PRICING_UNAVAILABLE
            }
        )

        return {
            "tenant_id": scoped_tenant_id,
            "task_count": task_count,
            "call_count": call_count,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "cost_usd": float(total_cost),
            "pricing_status": pricing_status,
            "unpriced_providers": unpriced_providers,
            "unpriced_models": unpriced_models,
            "models": models,
        }

    @staticmethod
    def _tenant_filter(tenant_id: int | None):
        if tenant_id is None:
            return True
        return LLMUsageRecord.tenant_id == int(tenant_id)
    
    def get_user_usage(
        self,
        user_id: int,
        since: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Get aggregated usage for a user.
        
        Args:
            user_id: ID of the user to get usage for
            since: Optional datetime to filter records from
            
        Returns:
            Dict with aggregated usage statistics
        """
        try:
            query = select(
                func.coalesce(func.sum(LLMUsageRecord.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsageRecord.completion_tokens), 0),
                func.coalesce(func.sum(LLMUsageRecord.total_tokens), 0),
                func.count(LLMUsageRecord.id),
            ).where(LLMUsageRecord.user_id == user_id)
            
            if since is not None:
                query = query.where(LLMUsageRecord.created_at >= since)
            
            result = self._db.execute(query).one()
            
            return {
                "user_id": user_id,
                "total_prompt_tokens": int(result[0]),
                "total_completion_tokens": int(result[1]),
                "total_tokens": int(result[2]),
                "call_count": int(result[3]),
                "since": since.isoformat() if since else None,
            }
            
        except Exception as exc:
            logger.warning(
                f"Failed to get user usage for user {user_id}: {exc}",
                exc_info=True,
            )
            return {
                "user_id": user_id,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_tokens": 0,
                "call_count": 0,
                "since": since.isoformat() if since else None,
            }


def _fill_missing_metadata_value(
    metadata: Dict[str, Any],
    key: str,
    value: str,
) -> None:
    """Fill metadata key when absent or still carrying the unknown sentinel."""

    normalized_value = str(value or "").strip()
    if not normalized_value:
        return
    current = metadata.get(key)
    if not isinstance(current, str) or not current.strip() or current == "unknown":
        metadata[key] = normalized_value


def _provider_model_label(provider: str, model: str) -> str:
    """Return a stable provider/model label for unpriced usage reporting."""

    provider_id = str(provider or "openai").strip().lower() or "openai"
    model_id = str(model or "unknown").strip() or "unknown"
    return f"{provider_id}/{model_id}"


__all__ = ["UsageTrackingService"]
