"""Validate memory extraction structured-output specs and schema consistency."""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if "core" not in sys.modules:
    core_pkg = types.ModuleType("core")
    core_pkg.__path__ = [str((ROOT_DIR / "core").resolve())]
    sys.modules["core"] = core_pkg

from agent.providers.llm.core.base import StructuredOutputSpec
from core.llm.structured_schemas import (
    MEMORY_EXTRACTION_STRUCTURED_OUTPUT,
    MEMORY_GATE_STRUCTURED_OUTPUT,
)
from backend.services.memory.memory_extraction_schemas import (
    ExtractionResult,
    GateClassifierOutput,
)


def test_gate_spec_is_valid_structured_output_spec() -> None:
    spec = MEMORY_GATE_STRUCTURED_OUTPUT
    assert isinstance(spec, StructuredOutputSpec)
    assert spec.name == "memory_gate"
    assert spec.strict is True
    assert spec.schema["required"] == ["extractable"]
    assert spec.schema["properties"]["extractable"]["type"] == "boolean"


def test_extraction_spec_is_valid_structured_output_spec() -> None:
    spec = MEMORY_EXTRACTION_STRUCTURED_OUTPUT
    assert isinstance(spec, StructuredOutputSpec)
    assert spec.name == "memory_extraction"
    assert spec.strict is True
    assert spec.schema["required"] == ["facts", "skipped_reason"]
    assert spec.schema["properties"]["facts"]["type"] == "array"


def test_gate_spec_schema_matches_pydantic_model() -> None:
    schema_properties = set(MEMORY_GATE_STRUCTURED_OUTPUT.schema["properties"].keys())
    model_fields = set(GateClassifierOutput.model_fields.keys())
    assert schema_properties == model_fields
    assert set(MEMORY_GATE_STRUCTURED_OUTPUT.schema["required"]) == {"extractable"}


def test_extraction_spec_schema_matches_pydantic_model() -> None:
    schema = MEMORY_EXTRACTION_STRUCTURED_OUTPUT.schema
    schema_properties = set(schema["properties"].keys())
    model_fields = set(ExtractionResult.model_fields.keys())
    assert schema_properties == model_fields
    assert set(schema["required"]) == {"facts", "skipped_reason"}

    fact_item = schema["properties"]["facts"]["items"]
    assert set(fact_item["required"]) == {"content", "tier"}
    assert fact_item["properties"]["tier"]["enum"] == ["user_profile", "task_engagement"]
