"""Behavioral tests for ``extract_last_tool_sections``.

Covers the public projection contract of
:func:`core.prompts.builders.post_tool.last_tool.extract_last_tool_sections`:

* Returned dict always has the ten documented keys, defaulting to ``""``.
* ``tool_executed`` resolves from ``facts.selected_tool`` then
  ``synthesized['tool']`` and never falls back to a placeholder.
* Parameter precedence: ``metadata['last_tool_result']['parameters']`` wins
  over ``facts.tool_parameters``; flat vs tool-keyed ``tool_parameters``
  shapes are handled correctly.
* ``summary`` and ``key_findings`` may fall back to ``synthesized`` while
  compact-only fields (``errors``, ``structured_signals``,
  ``decision_evidence``, ``artifact_refs``, ``lossiness_risk``) do not.
* Summary is truncated using the canonical ``MAX_SUMMARY_CHARS`` constant
  imported from ``_formatting`` (so the test follows the constant).
* Both mapping-style and object-style ``facts`` work.
* The helper does not mutate its inputs.

The tests use the public API only and never reach into private helpers.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

import pytest

from core.prompts.builders.post_tool._formatting import MAX_SUMMARY_CHARS
from core.prompts.builders.post_tool.evidence import EvidenceView
from core.prompts.builders.post_tool.last_tool import (
    LAST_TOOL_SECTION_HEADINGS,
    LAST_TOOL_SECTION_ORDER,
    extract_last_tool_sections,
)


EXPECTED_RESULT_KEY_ORDER = (
    "tool_executed",
    "tool_output_summary",
    "batch_tool_results",
    "key_findings",
    "tool_errors",
    "structured_signals",
    "decision_evidence",
    "artifact_refs",
    "compression_lossiness",
    "output_info",
)

EXPECTED_SECTION_ORDER = (
    "tool_executed",
    "tool_output_summary",
    "batch_tool_results",
    "key_findings",
    "tool_errors",
    "structured_signals",
    "decision_evidence",
    "compression_lossiness",
    "artifact_refs",
    "output_info",
)

EXPECTED_KEYS = set(EXPECTED_RESULT_KEY_ORDER)

EXPECTED_HEADINGS = {
    "tool_executed": "Tool Executed",
    "tool_output_summary": "Tool Output Summary",
    "batch_tool_results": "Batch Tool Results",
    "key_findings": "Key Findings",
    "tool_errors": "Tool Errors",
    "structured_signals": "Structured Signals",
    "decision_evidence": "Decision Evidence",
    "compression_lossiness": "Compression Lossiness",
    "artifact_refs": "Artifact References",
    "output_info": "Output Info",
}


class _ObjectFacts:
    """Minimal object-style facts double exposing attributes only.

    Mirrors the ``FactsState`` surface relevant to the helper
    (``selected_tool`` + ``tool_parameters``) without depending on the
    full pydantic model. Using a tiny class makes it explicit that
    ``get_field`` reaches the attributes via ``getattr``.
    """

    def __init__(
        self,
        *,
        selected_tool: Optional[str] = None,
        tool_parameters: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.selected_tool = selected_tool
        self.tool_parameters = tool_parameters if tool_parameters is not None else {}


def _mapping_facts(
    *,
    selected_tool: Optional[str] = None,
    tool_parameters: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a mapping-style facts payload (the other supported shape)."""
    facts: Dict[str, Any] = {}
    if selected_tool is not None:
        facts["selected_tool"] = selected_tool
    if tool_parameters is not None:
        facts["tool_parameters"] = tool_parameters
    return facts


def test_empty_metadata_and_facts_returns_all_ten_keys_empty() -> None:
    """Empty inputs return every documented key set to ``""``."""

    result = extract_last_tool_sections({}, _ObjectFacts())

    assert set(result.keys()) == EXPECTED_KEYS
    assert tuple(result.keys()) == EXPECTED_RESULT_KEY_ORDER
    for key in EXPECTED_KEYS:
        assert result[key] == "", f"expected empty string for {key}, got {result[key]!r}"


def test_last_tool_section_order_and_headings_are_stable() -> None:
    """Prompt-facing section names and order remain stable."""

    assert LAST_TOOL_SECTION_ORDER == EXPECTED_SECTION_ORDER
    assert tuple(LAST_TOOL_SECTION_HEADINGS.keys()) == EXPECTED_SECTION_ORDER
    assert LAST_TOOL_SECTION_HEADINGS == EXPECTED_HEADINGS


def test_no_selected_tool_and_no_synthesized_yields_empty_tool_executed() -> None:
    """No selected_tool and no synthesized.tool means tool_executed is ``""``.

    Specifically the helper must NOT emit ``"unknown tool"`` as PTR's
    inline path historically did.
    """

    result_no_synth = extract_last_tool_sections({}, _ObjectFacts(), synthesized=None)
    result_empty_synth = extract_last_tool_sections({}, _ObjectFacts(), synthesized={})

    assert result_no_synth["tool_executed"] == ""
    assert result_empty_synth["tool_executed"] == ""
    assert "unknown tool" not in result_no_synth["tool_executed"]
    assert "unknown tool" not in result_empty_synth["tool_executed"]


def test_whitespace_only_tool_names_are_treated_as_missing() -> None:
    """Whitespace-only tool names must not render placeholder-like sections."""

    result = extract_last_tool_sections(
        {},
        _ObjectFacts(selected_tool="   "),
        synthesized={"tool": "\t\n"},
    )

    assert result["tool_executed"] == ""


def test_tool_names_are_trimmed_before_rendering() -> None:
    """Resolved tool names are normalized before rendering ``Tool:``."""

    selected_result = extract_last_tool_sections(
        {},
        _ObjectFacts(selected_tool="  nmap.scan  "),
    )
    synthesized_result = extract_last_tool_sections(
        {},
        _ObjectFacts(selected_tool="  "),
        synthesized={"tool": "  http.probe  "},
    )

    assert selected_result["tool_executed"] == "Tool: nmap.scan"
    assert synthesized_result["tool_executed"] == "Tool: http.probe"


def test_selected_tool_without_params_renders_only_tool_line() -> None:
    """A tool name with no parameters renders just ``Tool: <name>``."""

    facts = _ObjectFacts(selected_tool="nmap.scan", tool_parameters={})

    result = extract_last_tool_sections({}, facts)

    assert result["tool_executed"] == "Tool: nmap.scan"
    assert "Parameters:" not in result["tool_executed"]


def test_metadata_last_tool_result_parameters_wins_over_facts_tool_parameters() -> None:
    """``metadata['last_tool_result']['parameters']`` precedence is honored."""

    metadata: Dict[str, Any] = {
        "last_tool_result": {
            "parameters": {"target": "10.0.0.1", "ports": "1-1024"},
        },
    }
    facts = _ObjectFacts(
        selected_tool="nmap.scan",
        tool_parameters={"target": "should-not-appear"},
    )

    result = extract_last_tool_sections(metadata, facts)

    assert result["tool_executed"].startswith("Tool: nmap.scan")
    assert "target=10.0.0.1" in result["tool_executed"]
    assert "ports=1-1024" in result["tool_executed"]
    assert "should-not-appear" not in result["tool_executed"]


def test_flat_facts_tool_parameters_fallback_renders_directly() -> None:
    """Flat ``{param: value}`` ``facts.tool_parameters`` is used as-is."""

    facts = _ObjectFacts(
        selected_tool="nmap.scan",
        tool_parameters={"target": "10.0.0.1", "rate": 100},
    )

    result = extract_last_tool_sections({}, facts)

    assert result["tool_executed"].startswith("Tool: nmap.scan")
    assert "target=10.0.0.1" in result["tool_executed"]
    assert "rate=100" in result["tool_executed"]


def test_tool_keyed_facts_tool_parameters_fallback_indexes_by_tool_name() -> None:
    """Tool-keyed ``{tool_name: {param: value}}`` is indexed by ``tool_name``.

    Only the entry for the resolved tool is rendered; sibling tool entries
    (for example, parameters for a different tool) do not leak.
    """

    facts = _ObjectFacts(
        selected_tool="nmap.scan",
        tool_parameters={
            "nmap.scan": {"target": "10.0.0.1", "ports": "80"},
            "other.tool": {"should": "not-appear"},
        },
    )

    result = extract_last_tool_sections({}, facts)

    assert result["tool_executed"].startswith("Tool: nmap.scan")
    assert "target=10.0.0.1" in result["tool_executed"]
    assert "ports=80" in result["tool_executed"]
    assert "should=not-appear" not in result["tool_executed"]
    assert "other.tool" not in result["tool_executed"]


def test_summary_truncates_with_max_summary_chars_constant() -> None:
    """Summary longer than ``MAX_SUMMARY_CHARS`` is truncated by the helper.

    The test references the constant by import rather than hardcoding the
    number so the test continues to track if the limit is later changed.
    """

    overflow_summary = "A" * (MAX_SUMMARY_CHARS + 250)
    metadata: Dict[str, Any] = {
        "last_tool_result_compact": {
            "summary": overflow_summary,
        },
    }

    result = extract_last_tool_sections(metadata, _ObjectFacts())

    truncated = result["tool_output_summary"]
    assert len(truncated) <= MAX_SUMMARY_CHARS
    # Truncation marker is the ellipsis character used by ``truncate``.
    assert truncated.endswith("…")
    # And a sub-limit summary is preserved verbatim.
    short_summary = "A" * (MAX_SUMMARY_CHARS - 100)
    short_metadata: Dict[str, Any] = {
        "last_tool_result_compact": {"summary": short_summary},
    }
    short_result = extract_last_tool_sections(short_metadata, _ObjectFacts())
    assert short_result["tool_output_summary"] == short_summary


def test_compact_summary_and_key_findings_win_over_synthesized() -> None:
    """When the compact result has summary/key_findings, synthesized is ignored."""

    metadata: Dict[str, Any] = {
        "last_tool_result_compact": {
            "summary": "compact-summary",
            "key_findings": ["compact-finding-1", "compact-finding-2"],
        },
    }
    synthesized = {
        "tool": "nmap.scan",
        "summary": "synth-summary",
        "key_findings": ["synth-finding"],
    }

    result = extract_last_tool_sections(metadata, _ObjectFacts(), synthesized=synthesized)

    assert result["tool_output_summary"] == "compact-summary"
    assert "synth-summary" not in result["tool_output_summary"]
    assert "compact-finding-1" in result["key_findings"]
    assert "compact-finding-2" in result["key_findings"]
    assert "synth-finding" not in result["key_findings"]


def test_synthesized_summary_and_key_findings_used_when_compact_missing() -> None:
    """Synthesized falls back only for ``summary`` / ``key_findings``."""

    synthesized = {
        "tool": "nmap.scan",
        "summary": "synth-summary",
        "key_findings": ["synth-finding-1", "synth-finding-2"],
    }

    result = extract_last_tool_sections({}, _ObjectFacts(), synthesized=synthesized)

    assert result["tool_output_summary"] == "synth-summary"
    assert "synth-finding-1" in result["key_findings"]
    assert "synth-finding-2" in result["key_findings"]
    # Synthesized.tool also resolves the tool name.
    assert result["tool_executed"] == "Tool: nmap.scan"


def test_compact_only_fields_do_not_use_synthesized_fallback() -> None:
    """Compact-only fields must come from compact metadata, not synthesized.
    """

    synthesized = {
        # Even if a caller mistakenly stuffs these into synthesized, the
        # helper must ignore them for the compact-only fields.
        "errors": ["should-not-appear-error"],
        "structured_signals": [{"id": "should-not-appear-signal"}],
        "decision_evidence": ["should-not-appear-evidence"],
        "artifact_refs": [{"artifact_id": "should-not-appear-artifact"}],
        "lossiness_risk": "should-not-appear-lossiness",
        "tool_errors": ["should-not-appear-tool-error"],
        "compression_lossiness": "should-not-appear-compression-lossiness",
    }

    result = extract_last_tool_sections({}, _ObjectFacts(), synthesized=synthesized)

    assert result["tool_errors"] == ""
    assert result["structured_signals"] == ""
    assert result["decision_evidence"] == ""
    assert result["artifact_refs"] == ""
    assert result["compression_lossiness"] == ""


def test_compact_only_fields_render_when_compact_provides_them() -> None:
    """Smoke test that compact data drives the compact-only fields."""

    metadata: Dict[str, Any] = {
        "last_tool_result_compact": {
            "errors": ["timeout while connecting"],
            "structured_signals": [{"signal": "port_open", "port": 80}],
            "decision_evidence": ["evidence-A", "evidence-B"],
            "artifact_refs": [
                {"artifact_id": "a-1", "label": "nmap output", "tool_name": "nmap.scan"},
            ],
            "lossiness_risk": "medium",
        },
    }

    result = extract_last_tool_sections(metadata, _ObjectFacts())

    assert "timeout while connecting" in result["tool_errors"]
    assert "port_open" in result["structured_signals"]
    assert "evidence-A" in result["decision_evidence"]
    assert "evidence-B" in result["decision_evidence"]
    assert "artifact_id=a-1" in result["artifact_refs"]
    assert result["compression_lossiness"] == "lossiness_risk: medium"


def test_mapping_style_and_object_style_facts_produce_equivalent_results() -> None:
    """Both mapping-style and object-style facts must yield the same output."""

    metadata: Dict[str, Any] = {
        "last_tool_result_compact": {
            "summary": "S",
            "key_findings": ["k1"],
        },
    }
    selected_tool = "nmap.scan"
    tool_parameters: Dict[str, Any] = {"target": "10.0.0.1"}

    object_result = extract_last_tool_sections(
        metadata,
        _ObjectFacts(selected_tool=selected_tool, tool_parameters=tool_parameters),
    )
    mapping_result = extract_last_tool_sections(
        metadata,
        _mapping_facts(selected_tool=selected_tool, tool_parameters=tool_parameters),
    )

    assert object_result == mapping_result
    # And SimpleNamespace, the other common object-style stand-in, must
    # also match — this documents the ``getattr``-based fallback path.
    namespace_result = extract_last_tool_sections(
        metadata,
        SimpleNamespace(selected_tool=selected_tool, tool_parameters=tool_parameters),
    )
    assert namespace_result == object_result


def test_helper_does_not_mutate_inputs() -> None:
    """Helper is read-only; inputs must equal their deep copies after a call."""

    metadata: Dict[str, Any] = {
        "last_tool_result": {"parameters": {"target": "10.0.0.1"}},
        "last_tool_result_compact": {
            "summary": "S",
            "key_findings": ["k1"],
            "errors": ["e1"],
            "structured_signals": [{"signal": "x"}],
            "decision_evidence": ["d1"],
            "artifact_refs": [{"artifact_id": "a1"}],
            "lossiness_risk": "low",
        },
        "last_artifact_path": "/workspace/.artifacts/nmap.txt",
    }
    metadata["last_tool_result"]["was_truncated"] = True
    metadata["last_tool_result"]["chars_truncated"] = 42
    metadata["last_tool_result"]["suggest_file_reading"] = False
    facts_params: Dict[str, Any] = {"nmap.scan": {"target": "10.0.0.1"}}
    synthesized: Dict[str, Any] = {
        "tool": "nmap.scan",
        "summary": "synth",
        "key_findings": ["sk"],
    }

    metadata_before = copy.deepcopy(metadata)
    facts_params_before = copy.deepcopy(facts_params)
    synthesized_before = copy.deepcopy(synthesized)

    facts = _ObjectFacts(selected_tool="nmap.scan", tool_parameters=facts_params)

    extract_last_tool_sections(metadata, facts, synthesized=synthesized)

    assert metadata == metadata_before
    assert facts_params == facts_params_before
    assert synthesized == synthesized_before
    # Object-style facts attributes are also unchanged.
    assert facts.selected_tool == "nmap.scan"
    assert facts.tool_parameters is facts_params


def test_batch_aware_compact_view_wins_over_legacy_field() -> None:
    """Phase 1.4: extractor reads the primary call's compact via the helper.

    When ``last_tool_result_compact_batch`` carries a primary call whose
    compact result has richer detail than the legacy
    ``last_tool_result_compact`` field, the extractor must surface the
    batch-derived detail.
    """
    metadata: Dict[str, Any] = {
        "last_tool_result_compact": {
            "summary": "stale legacy summary",
            "key_findings": [],
            "errors": [],
        },
        "last_tool_result_compact_batch": {
            "tool_batch_id": "tb_phase14",
            "execution_strategy": "sequential",
            "status": "completed_with_errors",
            "success": False,
            "results": [
                {
                    "tool_call_id": "tc_0",
                    "tool_id": "nmap.scan",
                    "intent": "discover",
                    "status": "failed",
                    "success": False,
                    "failure_category": "tool_error",
                    "compact_tool_result": {
                        "summary": "FRESH BATCH SUMMARY",
                        "key_findings": ["batch_finding"],
                        "errors": ["timeout"],
                    },
                }
            ],
        },
    }
    facts = _ObjectFacts(selected_tool="nmap.scan", tool_parameters={"target": "x"})

    result = extract_last_tool_sections(metadata, facts, synthesized=None)

    # The fresh batch-derived summary, not the stale legacy one.
    assert "FRESH BATCH SUMMARY" in result["tool_output_summary"]
    assert "stale legacy" not in result["tool_output_summary"]
    # Batch-derived findings/errors also surface.
    assert "batch_finding" in result["key_findings"]
    assert "timeout" in result["tool_errors"]
    assert result["batch_tool_results"] == (
        "batch_status: completed_with_errors\n"
        "batch_success: False\n"
        "- nmap.scan: failed; failure=tool_error; intent=discover; "
        "summary=FRESH BATCH SUMMARY"
    )


def test_extract_last_tool_sections_reads_through_compact_evidence_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extractor gets compact data from ``read_compact_evidence``."""

    metadata: Dict[str, Any] = {
        "last_tool_result_compact": {
            "summary": "metadata summary should be ignored by patched reader",
            "key_findings": ["metadata finding should be ignored"],
        },
    }
    calls: List[tuple[Mapping[str, Any], bool]] = []

    def fake_read_compact_evidence(
        observed_metadata: Mapping[str, Any],
        *,
        prefer_runtime: bool = False,
    ) -> EvidenceView:
        calls.append((observed_metadata, prefer_runtime))
        return EvidenceView(
            source="single",
            status="success",
            success=True,
            rows=(
                {
                    "tool_id": "patched.tool",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": {
                        "summary": "summary from evidence helper",
                        "key_findings": ["finding from evidence helper"],
                        "errors": ["error from evidence helper"],
                    },
                },
            ),
            successful_rows=(),
            failed_rows=(),
            raw={},
        )

    monkeypatch.setattr(
        "core.prompts.builders.post_tool.last_tool.read_compact_evidence",
        fake_read_compact_evidence,
    )

    result = extract_last_tool_sections(
        metadata,
        _ObjectFacts(),
        prefer_runtime_evidence=True,
    )

    assert calls == [(metadata, True)]
    assert result["tool_output_summary"] == "summary from evidence helper"
    assert "finding from evidence helper" in result["key_findings"]
    assert "error from evidence helper" in result["tool_errors"]
    assert "metadata summary should be ignored" not in result["tool_output_summary"]


def test_batch_tool_results_renders_all_rows_like_ptr_current_prompt() -> None:
    """Batch evidence body matches PTR's current batch result formatting."""

    long_summary = "S" * (MAX_SUMMARY_CHARS + 20)
    metadata: Dict[str, Any] = {
        "last_tool_result_compact_batch": {
            "status": "completed_with_errors",
            "success": False,
            "results": [
                {
                    "tool_call_id": "tc_0",
                    "tool_id": "nmap.scan",
                    "intent": "discover",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": {
                        "summary": "80/tcp open",
                    },
                },
                {
                    "tool_call_id": "tc_1",
                    "tool_id": "http.probe",
                    "intent": "fingerprint",
                    "status": "failed",
                    "success": False,
                    "failure_category": "timeout",
                    "compact_tool_result": {
                        "summary": long_summary,
                    },
                },
            ],
        },
    }

    result = extract_last_tool_sections(metadata, _ObjectFacts())

    assert result["batch_tool_results"].splitlines()[0:2] == [
        "batch_status: completed_with_errors",
        "batch_success: False",
    ]
    assert "- nmap.scan: success; intent=discover; summary=80/tcp open" in result[
        "batch_tool_results"
    ]
    assert (
        "- http.probe: failed; failure=timeout; intent=fingerprint; summary="
        in result["batch_tool_results"]
    )
    assert "S…" in result["batch_tool_results"]


def test_dual_lane_compact_rows_render_llm_then_deterministic() -> None:
    """Rows with both compact lanes render both lanes in stable order."""

    metadata: Dict[str, Any] = {
        "last_tool_result_compact_batch": {
            "status": "completed",
            "success": True,
            "results": [
                {
                    "tool_call_id": "tc_0",
                    "tool_id": "nmap.scan",
                    "intent": "discover",
                    "status": "success",
                    "success": True,
                    "compact_tool_result": {
                        "summary": "LLM summary",
                        "key_findings": ["LLM finding"],
                        "decision_evidence": ["LLM evidence"],
                        "lossiness_risk": "medium",
                    },
                    "deterministic_compact_tool_result": {
                        "summary": "Deterministic summary",
                        "key_findings": ["Deterministic finding"],
                        "decision_evidence": ["Deterministic evidence"],
                        "lossiness_risk": "low",
                    },
                }
            ],
        },
    }

    result = extract_last_tool_sections(metadata, _ObjectFacts())

    assert "llm_summary=LLM summary" in result["batch_tool_results"]
    assert (
        "deterministic_summary=Deterministic summary"
        in result["batch_tool_results"]
    )
    assert result["tool_output_summary"] == (
        "LLM lane:\nLLM summary\n"
        "Deterministic lane:\nDeterministic summary"
    )
    assert result["key_findings"] == (
        "LLM lane:\n• LLM finding\n"
        "Deterministic lane:\n• Deterministic finding"
    )
    assert result["decision_evidence"] == (
        "LLM lane:\n• LLM evidence\n"
        "Deterministic lane:\n• Deterministic evidence"
    )
    assert result["compression_lossiness"] == (
        "LLM lane:\nlossiness_risk: medium\n"
        "Deterministic lane:\nlossiness_risk: low"
    )


def test_output_info_renders_condensed_artifact_reading_guidance() -> None:
    """Output info points to visible filesystem follow-up when requested."""

    metadata: Dict[str, Any] = {
        "last_artifact_path": "/workspace/.artifacts/out.txt",
        "last_tool_result": {
            "was_truncated": True,
            "chars_truncated": 12345,
            "suggest_file_reading": True,
        },
    }

    result = extract_last_tool_sections(metadata, _ObjectFacts())

    assert result["output_info"] == (
        "Output condensed (12,345 chars omitted). "
        "If key evidence is still missing and the saved path is available, "
        "use a visible filesystem read/search tool with bounded scope. "
        "Do not default to full reads."
        "\nSaved output path: `/workspace/.artifacts/out.txt`"
    )


def test_output_info_renders_slight_condensation_guidance() -> None:
    """Output info mirrors PTR's low-risk condensation message."""

    metadata: Dict[str, Any] = {
        "last_artifact_path": "/workspace/.artifacts/out.txt",
        "last_tool_result": {
            "was_truncated": True,
            "chars_truncated": 987,
            "suggest_file_reading": False,
        },
    }

    result = extract_last_tool_sections(metadata, _ObjectFacts())

    assert result["output_info"] == (
        "Output slightly condensed (987 chars). "
        "Compact summary likely contains required evidence; avoid extra artifact "
        "reads unless a concrete gap remains."
        "\nSaved output path: `/workspace/.artifacts/out.txt`"
    )


def test_returned_dict_has_only_the_ten_documented_keys() -> None:
    """Defensive check that no extra keys creep into the projection contract."""

    metadata: Dict[str, Any] = {
        "last_tool_result_compact": {
            "summary": "S",
            "key_findings": ["k1"],
            "errors": ["e1"],
            "structured_signals": [{"signal": "x"}],
            "decision_evidence": ["d1"],
            "artifact_refs": [{"artifact_id": "a1"}],
        },
    }
    facts = _ObjectFacts(selected_tool="nmap.scan", tool_parameters={"target": "x"})

    result = extract_last_tool_sections(metadata, facts, synthesized={"tool": "nmap.scan"})

    assert set(result.keys()) == EXPECTED_KEYS
    # And every value remains a string (the helper's documented value type).
    assert all(isinstance(v, str) for v in result.values())


__all__: List[str] = []
