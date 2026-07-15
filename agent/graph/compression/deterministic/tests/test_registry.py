"""Unit tests for deterministic compression adapter registry lookup."""

from __future__ import annotations

import ast
from pathlib import Path

from agent.graph.compression.deterministic.contracts import (
    CompressionInput,
    DeterministicCompressionResult,
)
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
    register_adapter,
)


def test_missing_adapter_returns_no_result_without_throwing() -> None:
    """No registered adapter returns the standard fallback reason."""
    input_data = CompressionInput(
        tool_name="registry_tests.missing.tool",
        raw_result={"stdout": "ignored"},
    )

    assert get_adapter(input_data.tool_name) is None
    assert compress_deterministically(input_data) == DeterministicCompressionResult.none(
        fallback_reason="no_deterministic_adapter"
    )


def test_exact_tool_id_registration_wins_over_family_prefix() -> None:
    """Exact tool ids take precedence over registered family adapters."""

    def family_adapter(
        input_data: CompressionInput,
    ) -> DeterministicCompressionResult:
        return DeterministicCompressionResult(
            summary=f"family:{input_data.tool_name}",
            completeness="partial",
        )

    def exact_adapter(
        input_data: CompressionInput,
    ) -> DeterministicCompressionResult:
        return DeterministicCompressionResult(
            summary=f"exact:{input_data.tool_name}",
            completeness="complete",
            lossiness_risk="low",
        )

    register_adapter("registry_tests.family.", family_adapter)
    register_adapter("registry_tests.family.exact", exact_adapter)

    input_data = CompressionInput(
        tool_name="registry_tests.family.exact",
        raw_result={"stdout": "ignored"},
    )

    assert get_adapter("registry_tests.family.exact") is exact_adapter
    assert get_adapter("registry_tests.family.other") is family_adapter
    assert compress_deterministically(input_data).summary == (
        "exact:registry_tests.family.exact"
    )


def test_family_prefix_registration_accepts_wildcard_suffix() -> None:
    """Family adapters can be registered with a trailing wildcard suffix."""

    def adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
        return DeterministicCompressionResult(
            summary=input_data.tool_name,
            completeness="partial",
        )

    register_adapter("registry_tests.wildcard.*", adapter)

    assert get_adapter("registry_tests.wildcard.child") is adapter


def test_adapter_exceptions_do_not_escape_compression_wrapper() -> None:
    """Adapter failures degrade to an explicit no-result contract."""

    def broken_adapter(
        input_data: CompressionInput,
    ) -> DeterministicCompressionResult:
        raise RuntimeError("adapter failed")

    register_adapter("registry_tests.broken", broken_adapter)

    result = compress_deterministically(
        CompressionInput(
            tool_name="registry_tests.broken",
            raw_result={"stdout": "ignored"},
        )
    )

    assert result == DeterministicCompressionResult.none(
        fallback_reason="deterministic_adapter_error"
    )


def test_registry_imports_only_local_contracts_and_stdlib() -> None:
    """Registry import remains cheap and does not import tool modules."""
    registry_path = (
        Path(__file__).resolve().parents[1] / "registry.py"
    )
    tree = ast.parse(registry_path.read_text())

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.add(module)

    assert "agent.tools" not in imports
    assert imports <= {"__future__", "typing", ".contracts"}
