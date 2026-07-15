"""Unit tests for deterministic compression adapter contracts."""

from __future__ import annotations

import pytest

from agent.graph.compression.deterministic.contracts import (
    CompressionInput,
    DeterministicCompressionResult,
)


def test_compression_input_requires_mapping_raw_result() -> None:
    """Adapter input accepts only mapping-shaped tool results."""
    input_data = CompressionInput(
        tool_name="filesystem.read_file",
        raw_result={"stdout": "contents"},
        artifact_path="/workspace/result.txt",
        execution_id="exec-1",
    )

    assert input_data.tool_name == "filesystem.read_file"
    assert input_data.raw_result == {"stdout": "contents"}

    with pytest.raises(TypeError, match="raw_result must be a mapping"):
        CompressionInput(
            tool_name="utility.echo",
            raw_result=["not", "mapping"],  # type: ignore[arg-type]
        )


def test_result_normalizes_optional_lists_and_scalars_to_tuples() -> None:
    """Adapters can return lists, tuples, scalars, or None without mutable output."""
    signal = {"kind": "service", "port": 443}

    result = DeterministicCompressionResult(
        summary="Observed HTTPS service.",
        key_findings=["open port", 443, None],
        errors=None,  # type: ignore[arg-type]
        structured_signals=[signal],
        decision_evidence="nmap.xml:7:https",
        completeness="partial",
    )
    signal["port"] = 8443

    assert result.key_findings == ("open port", "443")
    assert result.errors == ()
    assert result.structured_signals == ({"kind": "service", "port": 443},)
    assert result.decision_evidence == ("nmap.xml:7:https",)


def test_none_constructor_returns_explicit_no_result() -> None:
    """Missing adapters can return a non-throwing explicit no-result contract."""
    result = DeterministicCompressionResult.none(
        fallback_reason="no_deterministic_adapter"
    )

    assert result == DeterministicCompressionResult(
        completeness="none",
        fallback_reason="no_deterministic_adapter",
    )
    assert result.key_findings == ()
    assert result.structured_signals == ()


@pytest.mark.parametrize("completeness", ["complete", "partial", "none"])
def test_completeness_accepts_documented_semantics(completeness: str) -> None:
    """Completeness is restricted to complete, partial, or none."""
    result = DeterministicCompressionResult(
        completeness=completeness,  # type: ignore[arg-type]
    )

    assert result.completeness == completeness


def test_result_rejects_invalid_completeness_and_lossiness() -> None:
    """Invalid semantic labels fail at construction time."""
    with pytest.raises(ValueError, match="completeness must be one of"):
        DeterministicCompressionResult(completeness="unknown")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="lossiness_risk must be one of"):
        DeterministicCompressionResult(lossiness_risk="none")  # type: ignore[arg-type]
