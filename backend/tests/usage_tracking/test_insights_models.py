"""Tests for the canonical usage-record metadata contract.

Purpose:
    Verify that ``UsageRecordMetadata`` enforces stable string defaults
    (never ``None``) for required fields, that ``turn_index`` remains
    optional, and that ``serialize_usage_metadata`` produces the exact
    JSON-safe shape stored in ``LLMUsageRecord.request_metadata``.

Boundaries:
    - This test only covers the contract defined in
      ``backend/services/usage_tracking/insights_models.py``. It does not
      exercise the LangGraph write path or any insights query layer; those
      are covered by Tasks 1.2+ and Phase 2.
"""

import json

import pytest

from backend.services.usage_tracking.insights_models import (
    UNKNOWN,
    UsageRecordMetadata,
    UsageRecordWithMetadata,
    build_usage_metadata_from_trace_record,
    role_and_node_from_source,
    serialize_usage_metadata,
)
from backend.services.usage_tracking.models import UsageData


class TestUsageRecordMetadataDefaults:
    """``UsageRecordMetadata`` must default every required field to ``"unknown"``."""

    def test_all_string_fields_default_to_unknown(self):
        meta = UsageRecordMetadata()

        assert meta.role == UNKNOWN
        assert meta.node_name == UNKNOWN
        assert meta.execution_branch == UNKNOWN
        assert meta.provider == UNKNOWN
        assert meta.api_surface == UNKNOWN
        assert meta.request_mode == UNKNOWN
        assert meta.cache_reporting == UNKNOWN

    def test_turn_index_defaults_to_none(self):
        # turn_index is the only field allowed to be None — insights treat
        # it as "not applicable" rather than a missing required value.
        meta = UsageRecordMetadata()
        assert meta.turn_index is None

    def test_unknown_sentinel_is_the_literal_string(self):
        # Downstream query code groups by this exact literal; guard against
        # accidental rename or capitalization drift.
        assert UNKNOWN == "unknown"

    def test_metadata_is_frozen(self):
        meta = UsageRecordMetadata()
        with pytest.raises(Exception):
            # frozen=True turns assignment into FrozenInstanceError; we
            # accept any exception subclass here to avoid coupling to the
            # exact dataclass exception type.
            meta.role = "planner"  # type: ignore[misc]

    def test_metadata_uses_slots(self):
        # slots=True + frozen=True together forbid both reassignment of
        # declared fields and addition of new ones. The exact exception
        # type depends on which guard fires first (frozen's __setattr__
        # raises before the slots descriptor would), so accept any
        # exception — the important invariant is that the assignment is
        # rejected, preventing call sites from smuggling untyped fields
        # through the contract.
        meta = UsageRecordMetadata()
        with pytest.raises(Exception):
            meta.extra_field = "nope"  # type: ignore[attr-defined]
        # Sanity check that __slots__ is actually defined on the class so
        # the protection is real (not just inherited from frozen=True).
        assert hasattr(UsageRecordMetadata, "__slots__")


class TestSerializeUsageMetadata:
    """``serialize_usage_metadata`` must emit a JSON-safe 1:1 dict."""

    def test_serializes_default_instance_with_unknown_strings(self):
        result = serialize_usage_metadata(UsageRecordMetadata())

        assert result == {
            "role": "unknown",
            "node_name": "unknown",
            "execution_branch": "unknown",
            "provider": "unknown",
            "api_surface": "unknown",
            "request_mode": "unknown",
            "cache_reporting": "unknown",
            "turn_index": None,
        }

    def test_serializes_fully_populated_metadata(self):
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

        result = serialize_usage_metadata(meta)

        assert result == {
            "role": "planner",
            "node_name": "tool_selector",
            "execution_branch": "tool",
            "provider": "openai",
            "api_surface": "chat_completions",
            "request_mode": "streaming",
            "cache_reporting": "reported",
            "turn_index": 2,
        }

    def test_result_is_json_serializable(self):
        meta = UsageRecordMetadata(
            role="finalizer",
            cache_reporting="not_reported",
            turn_index=0,
        )
        # Round-trip through json to confirm no non-primitive values leak in.
        encoded = json.dumps(serialize_usage_metadata(meta))
        decoded = json.loads(encoded)

        assert decoded["role"] == "finalizer"
        assert decoded["cache_reporting"] == "not_reported"
        assert decoded["turn_index"] == 0
        # Defaults that weren't overridden still appear as "unknown".
        assert decoded["provider"] == "unknown"

    def test_turn_index_zero_is_preserved_distinct_from_none(self):
        zero = serialize_usage_metadata(UsageRecordMetadata(turn_index=0))
        none = serialize_usage_metadata(UsageRecordMetadata(turn_index=None))

        assert zero["turn_index"] == 0
        assert none["turn_index"] is None

    def test_rejects_non_metadata_input(self):
        with pytest.raises(TypeError):
            serialize_usage_metadata({"role": "planner"})  # type: ignore[arg-type]


class TestRoleAndNodeFromSource:
    """``role_and_node_from_source`` is the single write-time normalizer.

    Downstream insights code must be able to group by ``role`` / ``node_name``
    without any further ``source`` parsing, so this mapping is where the
    legacy ``source`` string is translated exactly once.
    """

    def test_known_source_maps_to_canonical_role_and_node(self):
        assert role_and_node_from_source("simple_chat") == ("simple_chat", "simple_chat")
        assert role_and_node_from_source("select_tool_categories") == (
            "planner",
            "tool_selector",
        )
        assert role_and_node_from_source("deep_reasoning_finalizer") == (
            "finalizer",
            "deep_reasoning_finalizer",
        )
        assert role_and_node_from_source("intent_classifier") == (
            "intent_classifier",
            "intent_classifier",
        )

    def test_unknown_source_falls_back_to_unknown(self):
        assert role_and_node_from_source("something_new") == (UNKNOWN, UNKNOWN)

    def test_none_source_is_safe(self):
        assert role_and_node_from_source(None) == (UNKNOWN, UNKNOWN)
        assert role_and_node_from_source("") == (UNKNOWN, UNKNOWN)

    def test_active_code_path_sources_are_mapped(self):
        """Regression: real sources emitted by production code must be
        covered by the map so the insights page doesn't under-report the
        planner/finalizer buckets. Each entry here has a real emission
        site — see agent/reasoning/llm_tool_selection.py,
        agent/graph/nodes/finalize_results.py, and the two streaming
        adapters under agent/graph/nodes/post_tool_reasoning/streaming/.
        """
        assert role_and_node_from_source("planner_tool_selection") == (
            "planner",
            "tool_selector",
        )
        assert role_and_node_from_source("tool_output_compressor") == (
            "tool_output_compressor",
            "tool_output_compressor",
        )
        assert role_and_node_from_source("finalize_tool_results") == (
            "finalizer",
            "finalize_tool_results",
        )
        assert role_and_node_from_source("post_tool_reasoning_simple") == (
            "planner",
            "post_tool_reasoning_simple",
        )
        assert role_and_node_from_source("post_tool_reasoning_dr") == (
            "planner",
            "post_tool_reasoning_dr",
        )


class TestBuildUsageMetadataFromTraceRecord:
    """``build_usage_metadata_from_trace_record`` is the handler-boundary
    normalizer from the graph-level ``trace.usage_records`` dict to the
    canonical ``UsageRecordMetadata`` contract."""

    def test_populates_role_node_branch_and_provider_when_known(self):
        record = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "model": "gpt-4o-mini",
            "provider": "openai",
            "source": "simple_chat",
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
            turn_index=3,
        )

        assert meta.role == "simple_chat"
        assert meta.node_name == "simple_chat"
        assert meta.execution_branch == "simple_chat"
        assert meta.provider == "openai"
        assert meta.turn_index == 3
        # Fields not yet known at the handler boundary stay "unknown"
        assert meta.api_surface == UNKNOWN
        assert meta.request_mode == UNKNOWN
        assert meta.cache_reporting == UNKNOWN

    def test_unknown_source_produces_unknown_role_and_node(self):
        record = {"source": "brand_new_node", "provider": "openai"}

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="deep_reasoning",
            provider="openai",
        )

        assert meta.role == UNKNOWN
        assert meta.node_name == UNKNOWN
        # But execution_branch + provider still propagate because they are
        # known at the handler boundary.
        assert meta.execution_branch == "deep_reasoning"
        assert meta.provider == "openai"

    def test_missing_source_and_provider_fall_back_to_unknown(self):
        meta = build_usage_metadata_from_trace_record(
            {"prompt_tokens": 0, "completion_tokens": 0},
            execution_branch="unknown",
        )

        assert meta.role == UNKNOWN
        assert meta.node_name == UNKNOWN
        assert meta.execution_branch == UNKNOWN
        assert meta.provider == UNKNOWN
        assert meta.turn_index is None

    def test_record_provider_overrides_fallback_provider(self):
        record = {"source": "simple_chat", "provider": "anthropic"}

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
        )

        assert meta.provider == "anthropic"


class TestUsageRecordWithMetadataEnvelope:
    """The envelope keeps the per-call ``UsageData`` paired with its
    canonical ``UsageRecordMetadata`` so nothing is dropped between the
    handler and ``record_usage_list_best_effort``."""

    def test_envelope_carries_both_halves(self):
        usage = UsageData(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            model="gpt-4o-mini",
        )
        metadata = UsageRecordMetadata(
            role="planner", node_name="tool_selector", execution_branch="simple_tool"
        )
        envelope = UsageRecordWithMetadata(usage=usage, metadata=metadata)

        assert envelope.usage is usage
        assert envelope.metadata is metadata

    def test_envelope_is_frozen(self):
        envelope = UsageRecordWithMetadata(
            usage=UsageData(
                prompt_tokens=1, completion_tokens=1, total_tokens=2, model="m"
            ),
            metadata=UsageRecordMetadata(),
        )
        with pytest.raises(Exception):
            envelope.usage = UsageData(  # type: ignore[misc]
                prompt_tokens=0, completion_tokens=0, total_tokens=0, model="m"
            )


# ---------------------------------------------------------------------------
# Task 1.3 — Cache-reporting signal propagation into the canonical metadata.
# ---------------------------------------------------------------------------


class TestBuildUsageMetadataCacheReporting:
    """``build_usage_metadata_from_trace_record`` must honor the
    ``cache_reporting`` / ``api_surface`` signal set by
    ``UsageData.from_openai_*`` at the provider extraction boundary so the
    label travels all the way into ``LLMUsageRecord.request_metadata``
    without any re-parsing of the legacy ``source`` string.
    """

    def test_record_cache_reporting_reported_survives(self):
        # The provider extractor already stamped ``cache_reporting`` onto
        # the record dict (via ``UsageData.to_dict``). The builder must
        # honor it verbatim so insights can trust the label.
        record = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "model": "gpt-4o-mini",
            "provider": "openai",
            "source": "simple_chat",
            "api_surface": "chat_completions",
            "cache_reporting": "reported",
            "cached_tokens": 30,
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
            turn_index=1,
        )

        assert meta.cache_reporting == "reported"
        assert meta.api_surface == "chat_completions"
        assert meta.provider == "openai"

    def test_request_mode_survives_when_call_site_knows_it(self):
        record = {
            "provider": "openai",
            "source": "simple_chat",
            "api_surface": "chat_completions",
            "request_mode": "streaming",
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
        )

        assert meta.request_mode == "streaming"

    def test_invalid_request_mode_falls_back_to_unknown(self):
        record = {
            "provider": "openai",
            "source": "simple_chat",
            "request_mode": "streamish",
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
        )

        assert meta.request_mode == UNKNOWN

    def test_record_cache_reporting_reported_with_zero_cached_is_still_reported(self):
        # Honesty guarantee: ``cached_tokens == 0`` on a surface that
        # reports cache info stays labeled ``reported`` — not silently
        # degraded to ``not_reported``.
        record = {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "model": "gpt-4o-mini",
            "provider": "openai",
            "source": "simple_chat",
            "api_surface": "chat_completions",
            "cache_reporting": "reported",
            "cached_tokens": 0,
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
        )

        assert meta.cache_reporting == "reported"

    def test_record_cache_reporting_not_reported_survives(self):
        record = {
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
            "model": "gpt-5",
            "provider": "openai",
            "source": "simple_chat",
            "api_surface": "responses",
            "cache_reporting": "not_reported",
            "cached_tokens": 0,
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
        )

        assert meta.cache_reporting == "not_reported"
        assert meta.api_surface == "responses"

    def test_falls_back_to_classifier_when_only_surface_is_present(self):
        # Some historical / fallback code paths may set ``api_surface``
        # without a ``cache_reporting`` literal. The builder should
        # derive the label from ``(provider, api_surface)`` via the
        # classifier rather than defaulting to ``unknown`` when the
        # combination IS actually classifiable.
        record = {
            "provider": "openai",
            "source": "simple_chat",
            "api_surface": "chat_completions",
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
        )

        assert meta.api_surface == "chat_completions"
        assert meta.cache_reporting == "reported"

    def test_unknown_provider_surface_stays_unknown(self):
        # Missing signal + unclassified surface must produce the
        # explicit ``"unknown"`` bucket — never silently ``"not_reported"``
        # or ``"reported"``.
        record = {
            "provider": "",
            "source": "simple_chat",
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
        )

        assert meta.cache_reporting == UNKNOWN
        assert meta.api_surface == UNKNOWN

    def test_stray_cache_reporting_string_falls_back_to_classifier(self):
        # Guard: only the three known literals are accepted. A typo'd
        # label must not poison the contract.
        record = {
            "provider": "openai",
            "source": "simple_chat",
            "api_surface": "chat_completions",
            "cache_reporting": "yes",  # not a known literal
        }

        meta = build_usage_metadata_from_trace_record(
            record,
            execution_branch="simple_chat",
            provider="openai",
        )

        # Fell back to classifier output, which recognizes the surface.
        assert meta.cache_reporting == "reported"
