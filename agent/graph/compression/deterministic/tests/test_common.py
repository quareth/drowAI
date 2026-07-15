"""Unit tests for family-neutral deterministic compression helpers."""

from __future__ import annotations

from core.prompts.constants import (
    COMPACT_DECISION_EVIDENCE_MAX_CHARS,
    COMPACT_SUMMARY_MAX_CHARS,
)

from agent.graph.compression.deterministic.contracts import (
    CompressionInput,
    DeterministicCompressionResult,
)
from agent.graph.compression.deterministic.common import (
    _metadata_compact_decision_evidence,
    _metadata_compact_key_findings,
    _metadata_compact_summary,
    as_int,
    build_deterministic_summary,
    build_usage_record,
    compact_evidence_line,
    dedupe_string_list,
    extract_token_usage,
    metadata_compact_adapter,
    register_metadata_compact_adapter,
    sanitize_artifact_refs,
)
from agent.graph.compression.deterministic.registry import compress_deterministically


def test_as_int_returns_none_for_absent_and_invalid_values() -> None:
    """Integer coercion preserves current None and invalid-value behavior."""
    assert as_int(None) is None
    assert as_int("not-an-int") is None
    assert as_int(["1"]) is None
    assert as_int("7") == 7


def test_dedupe_string_list_trims_dedupes_and_obeys_limit() -> None:
    """String list normalization keeps first-seen order and current limit behavior."""
    values = [" alpha ", "beta", "", "alpha", None, "gamma", "delta"]

    assert dedupe_string_list(values, limit=3) == ["alpha", "beta", "None"]
    assert dedupe_string_list(values, limit=None) == [
        "alpha",
        "beta",
        "None",
        "gamma",
        "delta",
    ]


def test_compact_evidence_line_flattens_multiline_text_and_applies_cap() -> None:
    """Decision evidence compaction keeps one bounded line with an ellipsis."""
    multiline = " first line \n\n second line \n third line "
    assert compact_evidence_line(multiline) == "first line second line third line"
    assert compact_evidence_line(None) == ""

    long_text = "x" * (COMPACT_DECISION_EVIDENCE_MAX_CHARS + 10)
    compact = compact_evidence_line(long_text)

    assert compact.endswith("...")
    assert len(compact) == COMPACT_DECISION_EVIDENCE_MAX_CHARS


def test_extract_token_usage_drops_none_and_invalid_values() -> None:
    """Token usage projection keeps only values coercible with int()."""
    assert extract_token_usage(None) is None
    assert extract_token_usage({"prompt_tokens": None, "bad": "abc"}) is None
    assert extract_token_usage(
        {
            "prompt_tokens": "5",
            "completion_tokens": 2.0,
            "bad": object(),
        }
    ) == {"prompt_tokens": 5, "completion_tokens": 2}


def test_build_usage_record_adds_canonical_compressor_metadata() -> None:
    """Usage record metadata overwrites caller values with compressor-owned fields."""
    usage = {
        "prompt_tokens": 5,
        "source": "upstream",
        "request_mode": "streaming",
    }

    assert build_usage_record(None) is None
    assert build_usage_record(usage) == {
        "prompt_tokens": 5,
        "source": "tool_output_compressor",
        "request_mode": "non_streaming",
    }
    assert usage["source"] == "upstream"


def test_sanitize_artifact_refs_prefers_stable_handles_and_dedupes_paths() -> None:
    """Artifact refs drop unsafe path material before envelope construction."""
    signed_url = (
        "https://objects.example.invalid/private/result.json"
        "?X-Amz-Signature=dummy-signature&X-Amz-Credential=dummy-credential"
    )

    assert sanitize_artifact_refs(
        [
            {
                "path": signed_url,
                "artifact_id": "artifact-signed",
                "artifact_kind": "object_store",
                "label": "Signed URL",
                "relative_path": "artifacts/result.json",
                "upload_status": "ready",
            },
            {
                "path": "tenant-a/task-123/private/result.json",
                "artifact_id": "artifact-object-key",
                "artifact_kind": "object_store",
                "relative_path": "tenant-a/task-123/private/result.json",
            },
            {
                "path": "/workspace/artifacts/local.txt",
                "execution_id": "exec-1",
            },
            {
                "path": signed_url,
                "artifact_id": "artifact-signed-duplicate",
                "artifact_kind": "object_store",
                "relative_path": "artifacts/result.json",
            },
        ]
    ) == [
        {
            "path": "artifacts/result.json",
            "artifact_id": "artifact-signed",
            "artifact_kind": "object_store",
            "label": "Signed URL",
            "relative_path": "artifacts/result.json",
        },
        {
            "path": "artifact://artifact-object-key",
            "artifact_id": "artifact-object-key",
            "artifact_kind": "object_store",
        },
        {
            "path": "/workspace/artifacts/local.txt",
            "execution_id": "exec-1",
        },
    ]


def test_build_deterministic_summary_preserves_precedence_and_bounds() -> None:
    """Fallback summary selection keeps current field precedence and length cap."""
    assert (
        build_deterministic_summary(
            {"observation": " observed ", "stdout_excerpt": "stdout"},
            combined_output="combined",
        )
        == "observed"
    )
    assert (
        build_deterministic_summary({"stdout_excerpt": " stdout "}, combined_output="combined")
        == "stdout"
    )
    assert (
        build_deterministic_summary({"stderr_excerpt": " stderr "}, combined_output="combined")
        == "stderr"
    )

    combined = "z" * (COMPACT_SUMMARY_MAX_CHARS + 10)
    summary = build_deterministic_summary({}, combined_output=combined)

    assert summary == "z" * COMPACT_SUMMARY_MAX_CHARS
    assert (
        build_deterministic_summary({}, combined_output="")
        == "Tool execution completed without textual output."
    )


def test_metadata_compact_summary_reads_tool_authored_metadata() -> None:
    """Tool-authored compact summaries keep current trim and absent-value behavior."""
    assert _metadata_compact_summary({"metadata": {"compact_summary": " summary "}}) == "summary"
    assert _metadata_compact_summary({"metadata": {"compact_summary": ""}}) == ""
    assert _metadata_compact_summary({"metadata": "not-a-dict"}) == ""
    assert _metadata_compact_summary({}) == ""


def test_metadata_compact_key_findings_are_unbounded_and_deduped() -> None:
    """Tool-authored key findings stay unbounded except normalization/de-dupe."""
    values = [
        " finding-1 ",
        "finding-2",
        "finding-3",
        "finding-4",
        "finding-5",
        "finding-6",
        "finding-2",
        "",
    ]

    assert _metadata_compact_key_findings(
        {"metadata": {"compact_key_findings": values}}
    ) == [
        "finding-1",
        "finding-2",
        "finding-3",
        "finding-4",
        "finding-5",
        "finding-6",
    ]
    assert _metadata_compact_key_findings(
        {"metadata": {"compact_key_findings": "not-a-list"}}
    ) == []
    assert _metadata_compact_key_findings({"metadata": {}}) == []


def test_metadata_compact_decision_evidence_compacts_lines_and_dedupes() -> None:
    """Tool-authored decision evidence keeps current line compaction and de-dupe."""
    long_text = "x" * (COMPACT_DECISION_EVIDENCE_MAX_CHARS + 10)

    evidence = _metadata_compact_decision_evidence(
        {
            "metadata": {
                "compact_decision_evidence": [
                    " first line \n second line ",
                    "first line \n second line",
                    long_text,
                    "",
                ]
            }
        }
    )

    assert evidence == [
        "first line second line",
        long_text[: COMPACT_DECISION_EVIDENCE_MAX_CHARS - 3] + "...",
    ]
    assert _metadata_compact_decision_evidence(
        {"metadata": {"compact_decision_evidence": "not-a-list"}}
    ) == []
    assert _metadata_compact_decision_evidence({"metadata": {}}) == []


def test_metadata_compact_adapter_projects_tool_authored_metadata() -> None:
    """Adapter exposes current metadata compact fields as partial facts."""
    result = metadata_compact_adapter(
        CompressionInput(
            tool_name="metadata_compact_tests.tool",
            raw_result={
                "metadata": {
                    "compact_summary": " summary ",
                    "compact_key_findings": [" finding ", "finding", ""],
                    "compact_decision_evidence": [
                        " first line \n second line ",
                        "first line \n second line",
                    ],
                }
            },
        )
    )

    assert result == DeterministicCompressionResult(
        summary="summary",
        key_findings=("finding",),
        decision_evidence=("first line second line",),
        completeness="partial",
    )


def test_metadata_compact_adapter_returns_none_without_metadata_fields() -> None:
    """Missing compact metadata remains an explicit no-result adapter outcome."""
    result = metadata_compact_adapter(
        CompressionInput(
            tool_name="metadata_compact_tests.empty",
            raw_result={"metadata": {"other": "ignored"}},
        )
    )

    assert result == DeterministicCompressionResult.none(
        fallback_reason="no_compact_metadata",
    )


def test_register_metadata_compact_adapter_uses_existing_registry() -> None:
    """Metadata compact registration returns partial adapter fields for a tool."""
    register_metadata_compact_adapter("metadata_compact_tests.registered")

    result = compress_deterministically(
        CompressionInput(
            tool_name="metadata_compact_tests.registered",
            raw_result={"metadata": {"compact_summary": "registered summary"}},
        )
    )

    assert result.summary == "registered summary"
    assert result.completeness == "partial"
