"""Canonical metadata contract for usage-record insights.

Purpose:
    Defines the single typed shape (`UsageRecordMetadata`) that every newly
    persisted `LLMUsageRecord.request_metadata` row carries. This is the
    backend-of-record for per-call descriptive context (role, node_name,
    execution_branch, provider, api_surface, request_mode, cache_reporting,
    optional turn_index) used by the Usage Insights query layer.

Responsibility:
    - Declare the canonical metadata fields with stable string defaults so
      historical or partially populated rows always group into an explicit
      `"unknown"` bucket rather than `None` / missing keys.
    - Provide a JSON-safe serializer helper that converts the dataclass into
      the dict shape stored in `LLMUsageRecord.request_metadata` without
      losing or renaming any field.
    - Provide the typed envelope (`UsageRecordWithMetadata`) that keeps
      `UsageData` and `UsageRecordMetadata` paired through
      `LangGraphChatResult.usage` and `record_usage_list_best_effort()` so
      handlers stop narrowing rich per-call dicts to plain ``UsageData``.
    - Map the per-call-site ``source`` string captured in
      ``trace.usage_records`` (e.g. ``"planner"``, ``"simple_chat"``,
      ``"decision_router"``) to the canonical ``role`` / ``node_name``
      values exactly once, at write time, so the read side never re-parses
      ``source`` strings.

Boundaries:
    - This module owns the contract only. It does not read from or write to
      the database and does not compute any aggregations. Wiring into the
      LangGraph write path lives in Task 1.2 (turn_execution_service /
      handlers); read-side aggregation lives in the future
      ``insights_query_service.py`` (Phase 2).
    - It must not import handler, router, or ORM modules to keep the
      contract dependency-free and trivially importable from any layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

if TYPE_CHECKING:
    from backend.services.usage_tracking.models import UsageData


# Sentinel default applied to every required string field. Centralized so a
# future rename (e.g. to "unspecified") only changes one place, and so query
# code can rely on a single literal when bucketing missing metadata.
UNKNOWN: str = "unknown"
_KNOWN_REQUEST_MODES: frozenset[str] = frozenset({"streaming", "non_streaming"})


@dataclass(slots=True, frozen=True)
class UsageRecordMetadata:
    """Per-call descriptive context for one ``LLMUsageRecord``.

    Every field is required to have a stable, non-``None`` default so that
    historical rows or partially populated handlers still serialize into a
    valid contract. Downstream insights grouping treats ``"unknown"`` as a
    real bucket, never as a missing value.

    Fields:
        role: Logical caller role within the LangGraph turn (e.g.
            ``"intent_classifier"``, ``"planner"``, ``"simple_chat"``,
            ``"finalizer"``). Used as the primary grouping key in insights;
            replaces ad-hoc parsing of the legacy ``source`` string.
        node_name: LangGraph node identifier that produced the call (e.g.
            ``"tool_selector"``). Finer-grained than ``role`` and stable
            across runs of the same graph.
        execution_branch: Branch / mode of the turn execution path (e.g.
            ``"normal"``, ``"tool"``, ``"interrupt_resume"``). Lets insights
            distinguish tool-using turns from chat-only turns.
        provider: LLM provider name (e.g. ``"openai"``). Mirrored from the
            underlying ``UsageData.provider`` so insights can group without
            re-deriving it from the model string.
        api_surface: Provider API surface that produced the call (e.g.
            ``"chat_completions"``, ``"responses"``). Required so cache
            reporting can be interpreted correctly per surface.
        request_mode: Streaming / batching mode of the call (e.g.
            ``"streaming"``, ``"non_streaming"``). Kept as metadata for
            future cache-behavior analysis; not used for cost math.
        cache_reporting: Honest label for whether the call's API surface
            actually reports cache info. One of ``"reported"``,
            ``"not_reported"``, or ``"unknown"`` — never silently ``"0"``
            for surfaces that do not expose cache details.
        turn_index: Optional zero-based turn index within the conversation
            that produced this call. Optional because not every call site
            knows its turn index (e.g. one-shot helpers); insights treat
            ``None`` as "not applicable".
    """

    role: str = UNKNOWN
    node_name: str = UNKNOWN
    execution_branch: str = UNKNOWN
    provider: str = UNKNOWN
    api_surface: str = UNKNOWN
    request_mode: str = UNKNOWN
    cache_reporting: str = UNKNOWN
    turn_index: int | None = None


def serialize_usage_metadata(metadata: UsageRecordMetadata) -> Dict[str, Any]:
    """Serialize a ``UsageRecordMetadata`` into the JSON-safe dict shape.

    This is the canonical conversion used before persisting metadata into
    ``LLMUsageRecord.request_metadata`` (a SQLAlchemy ``JSON`` column). The
    output preserves every field name 1:1 with the dataclass so the read
    path can rebuild the contract without any mapping table.

    All string fields default to ``"unknown"`` (never ``None``); only
    ``turn_index`` may be ``None`` and is preserved as JSON ``null`` so
    insights queries can distinguish "not applicable" from a real index of
    ``0``.

    Args:
        metadata: The canonical metadata for a single LLM call.

    Returns:
        A plain ``dict[str, Any]`` containing exactly the dataclass fields,
        safe to hand to SQLAlchemy's JSON column, ``json.dumps``, or any
        Pydantic response model.

    Example:
        meta = UsageRecordMetadata(
            role="planner",
            node_name="tool_selector",
            execution_branch="tool",
            provider="openai",
            api_surface="chat_completions",
            request_mode="streaming",
            cache_reporting="reported",
            turn_index=2,
        )
        request_metadata = serialize_usage_metadata(meta)
        # {
        #   "role": "planner",
        #   "node_name": "tool_selector",
        #   "execution_branch": "tool",
        #   "provider": "openai",
        #   "api_surface": "chat_completions",
        #   "request_mode": "streaming",
        #   "cache_reporting": "reported",
        #   "turn_index": 2,
        # }
    """
    if not isinstance(metadata, UsageRecordMetadata):
        raise TypeError(
            "serialize_usage_metadata expects a UsageRecordMetadata instance, "
            f"got {type(metadata).__name__}"
        )
    # asdict() is safe here: the dataclass only contains primitive fields
    # (str / int / None), so the result is already JSON-serializable.
    return asdict(metadata)


# ---------------------------------------------------------------------------
# Envelope used to carry metadata alongside ``UsageData`` through the real
# LangGraph write path without narrowing the per-call captures.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class UsageRecordWithMetadata:
    """Pair of a single ``UsageData`` and its canonical ``UsageRecordMetadata``.

    Used by LangGraph handlers to hand per-call usage to
    ``record_usage_list_best_effort()`` without dropping metadata. The
    persistence bridge unwraps each envelope back into
    ``UsageTrackingService.record_usage()`` with the attached metadata
    serialized into ``LLMUsageRecord.request_metadata``.

    This envelope is intentionally tiny: it does not own any transformation
    logic — that belongs to the call site that constructs it (see
    ``build_usage_metadata_from_trace_record``) — and it does not duplicate
    the fields of ``UsageData`` or ``UsageRecordMetadata``. Keeping the two
    parts intact means callers and tests can inspect either half with no
    unwrapping ambiguity.
    """

    usage: "UsageData"
    metadata: UsageRecordMetadata


# ---------------------------------------------------------------------------
# Mapping from the per-call-site ``source`` string captured in
# ``trace.usage_records`` to the canonical ``(role, node_name)`` pair.
#
# This mapping is the ONLY place the legacy ``source`` string is parsed. It
# is applied once at write time so every downstream consumer (insights
# groups/timeline/records, frontend charts) can rely on ``role`` /
# ``node_name`` as first-class keys.
# ---------------------------------------------------------------------------


# Known sources -> (role, node_name). ``node_name`` is the LangGraph node /
# call-site identifier; ``role`` is the product-level grouping key used by
# insights. For simple paths the two coincide (e.g. ``simple_chat``); for
# the planner family the role is ``"planner"`` while ``node_name`` captures
# which specific planner call (tool selection, parameter resolution, etc.).
_SOURCE_ROLE_MAP: Dict[str, tuple[str, str]] = {
    # Intent analyzer - runs outside the graph, injected into initial state
    "intent_classifier": ("intent_classifier", "intent_classifier"),
    # Simple chat branch - one LLM call producing the final answer
    "simple_chat": ("simple_chat", "simple_chat"),
    # Deep reasoning branch
    "decision_router": ("planner", "decision_router"),
    "think_more": ("planner", "think_more"),
    "think_more_fallback": ("planner", "think_more"),
    "reflect": ("planner", "reflect"),
    "synthesis": ("finalizer", "synthesis"),
    "deep_reasoning_finalizer": ("finalizer", "deep_reasoning_finalizer"),
    # Simple tool / tool execution branch
    "select_tool_categories": ("planner", "tool_selector"),
    "planner_tool_selection": ("planner", "tool_selector"),
    "planner": ("planner", "planner"),
    "planner_parameter_generation": ("planner", "parameter_resolver"),
    "tool_output_compressor": ("tool_output_compressor", "tool_output_compressor"),
    "tool_articulation": ("finalizer", "tool_articulation"),
    "finalize_tool_results": ("finalizer", "finalize_tool_results"),
    "post_tool_observation": ("planner", "post_tool_observation"),
    "post_tool_analysis": ("planner", "post_tool_analysis"),
    "post_tool_reasoning": ("planner", "post_tool_reasoning"),
    "post_tool_reasoning_simple": ("planner", "post_tool_reasoning_simple"),
    "post_tool_reasoning_dr": ("planner", "post_tool_reasoning_dr"),
}


def role_and_node_from_source(source: Optional[str]) -> tuple[str, str]:
    """Map a legacy per-call ``source`` string to ``(role, node_name)``.

    Returns ``(UNKNOWN, UNKNOWN)`` for anything not in the known map so the
    insights layer groups genuinely unrecognized sources into the
    ``"unknown"`` bucket rather than inventing a role from the raw string.

    Args:
        source: Call-site identifier as written into
            ``trace.usage_records[*]["source"]`` (e.g. ``"simple_chat"``,
            ``"planner"``). May be ``None`` or missing for historical rows.

    Returns:
        Tuple ``(role, node_name)`` where each element is always a
        non-``None`` string — ``UNKNOWN`` when no mapping exists.
    """
    if not isinstance(source, str) or not source:
        return UNKNOWN, UNKNOWN
    return _SOURCE_ROLE_MAP.get(source, (UNKNOWN, UNKNOWN))


def build_usage_metadata_from_trace_record(
    record: Mapping[str, Any],
    *,
    execution_branch: str = UNKNOWN,
    provider: str = UNKNOWN,
    turn_index: Optional[int] = None,
) -> UsageRecordMetadata:
    """Build ``UsageRecordMetadata`` from one ``trace.usage_records`` dict.

    This normalizes the dict shape produced by ``_usage_to_dict`` (see
    ``agent/graph/nodes/node_utils.py``) into the canonical metadata
    contract. ``api_surface`` and ``cache_reporting`` are lifted from the
    record dict when present (populated at the provider-extraction
    boundary by ``UsageData.from_openai_*``); otherwise they fall back to
    the ``classify_cache_reporting`` classifier with whatever signal is
    available, and finally to ``"unknown"`` so legacy rows group into the
    explicit unknown bucket instead of masquerading as ``0 cache``.

    ``request_mode`` is lifted from the record when the call site knows it
    (currently ``"streaming"`` or ``"non_streaming"``), otherwise it stays in
    the explicit ``"unknown"`` bucket for historical rows and legacy paths.

    Args:
        record: A single ``trace.usage_records`` entry.
        execution_branch: Branch of the turn that produced the call, e.g.
            ``"simple_chat"``, ``"deep_reasoning"``, ``"simple_tool"``. Set
            by the handler that extracts the records.
        provider: Provider name from the surrounding ``UsageData``; falls
            back to ``"unknown"`` when the record dict does not name one.
        turn_index: Optional turn index within the conversation. Handlers
            populate it from ``turn_number`` / ``turn_sequence`` when known.

    Returns:
        ``UsageRecordMetadata`` with ``role``, ``node_name``,
        ``execution_branch``, ``provider``, ``api_surface`` and
        ``cache_reporting`` populated whenever possible and every other
        required field defaulting to ``"unknown"``.
    """
    # Import locally to avoid a module-level cycle: ``models`` does not
    # depend on ``insights_models``, and this module is imported by write
    # paths that also touch ``models``. The import cost is trivial here
    # (happens once per persisted usage record).
    from backend.services.usage_tracking.models import (
        CACHE_REPORTING_UNKNOWN,
        classify_cache_reporting,
    )

    source = record.get("source") if isinstance(record, Mapping) else None
    role, node_name = role_and_node_from_source(source if isinstance(source, str) else None)

    record_provider = record.get("provider") if isinstance(record, Mapping) else None
    resolved_provider = provider
    if isinstance(record_provider, str) and record_provider:
        resolved_provider = record_provider
    if not isinstance(resolved_provider, str) or not resolved_provider:
        resolved_provider = UNKNOWN

    # Prefer an explicit ``api_surface`` on the record (set by the
    # provider-level extractor in ``UsageData.from_openai_*``). Fall back
    # to ``"unknown"`` — the classifier treats that as unclassified.
    record_surface = record.get("api_surface") if isinstance(record, Mapping) else None
    if isinstance(record_surface, str) and record_surface:
        resolved_surface = record_surface
    else:
        resolved_surface = UNKNOWN

    # Cache-reporting: prefer an explicitly labeled value coming from the
    # extractor (this is the honest, source-of-truth path), else derive
    # via the classifier from ``(provider, api_surface)``. Only accept
    # the three known literals to guard against stray values.
    record_cache_reporting = (
        record.get("cache_reporting") if isinstance(record, Mapping) else None
    )
    if record_cache_reporting in ("reported", "not_reported", "unknown"):
        resolved_cache_reporting = record_cache_reporting
    else:
        resolved_cache_reporting = classify_cache_reporting(
            resolved_provider, resolved_surface
        )
    if not isinstance(resolved_cache_reporting, str) or not resolved_cache_reporting:
        resolved_cache_reporting = CACHE_REPORTING_UNKNOWN

    record_request_mode = (
        record.get("request_mode") if isinstance(record, Mapping) else None
    )
    resolved_request_mode = (
        record_request_mode if record_request_mode in _KNOWN_REQUEST_MODES else UNKNOWN
    )

    return UsageRecordMetadata(
        role=role,
        node_name=node_name,
        execution_branch=execution_branch or UNKNOWN,
        provider=resolved_provider,
        api_surface=resolved_surface,
        request_mode=resolved_request_mode,
        cache_reporting=resolved_cache_reporting,
        turn_index=turn_index,
    )


__all__ = [
    "UNKNOWN",
    "UsageRecordMetadata",
    "UsageRecordWithMetadata",
    "build_usage_metadata_from_trace_record",
    "role_and_node_from_source",
    "serialize_usage_metadata",
]
