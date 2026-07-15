"""Tests for neutral structured-output parsing and OpenAI payload helpers."""

from __future__ import annotations

import pytest

from agent.providers.llm.core.base import StructuredOutputSpec
from agent.providers.llm.adapters.openai.structured_output import (
    StructuredOutputSchemaError,
    build_chat_response_format,
    build_responses_text_format,
    validate_openai_strict_schema,
)
from agent.providers.llm.contracts.structured_output import (
    StructuredOutputParseError,
    parse_structured_content,
)


def _host_port_spec() -> StructuredOutputSpec:
    """Return a strict object schema used by structured-output tests."""
    return StructuredOutputSpec(
        name="host_port",
        schema={
            "type": "object",
            "properties": {
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
            "required": ["host", "port"],
            "additionalProperties": False,
        },
    )


def test_parse_structured_content_validates_json_object() -> None:
    parsed = parse_structured_content(
        '{"host":"192.168.1.10","port":443}',
        _host_port_spec(),
    )

    assert parsed == {"host": "192.168.1.10", "port": 443}


def test_parse_structured_content_accepts_fenced_json_object() -> None:
    parsed = parse_structured_content(
        '```json\n{"host":"192.168.1.10","port":443}\n```',
        _host_port_spec(),
    )

    assert parsed == {"host": "192.168.1.10", "port": 443}


def test_parse_structured_content_accepts_single_embedded_json_object() -> None:
    parsed = parse_structured_content(
        'Here is the result:\n{"host":"192.168.1.10","port":443}\nDone.',
        _host_port_spec(),
    )

    assert parsed == {"host": "192.168.1.10", "port": 443}


def test_parse_structured_content_rejects_non_json() -> None:
    with pytest.raises(StructuredOutputParseError) as exc_info:
        parse_structured_content("not-json", _host_port_spec())

    assert exc_info.value.reason == "json_decode_error"


def test_parse_structured_content_rejects_schema_mismatch() -> None:
    with pytest.raises(StructuredOutputParseError) as exc_info:
        parse_structured_content('{"host":"192.168.1.10"}', _host_port_spec())

    assert exc_info.value.reason == "schema_validation_error"


def test_parse_structured_content_rejects_ambiguous_embedded_json_objects() -> None:
    with pytest.raises(StructuredOutputParseError) as exc_info:
        parse_structured_content(
            '{"host":"192.168.1.10","port":443}\n{"host":"10.0.0.2","port":80}',
            _host_port_spec(),
        )

    assert exc_info.value.reason == "ambiguous_json_object"


def test_openai_chat_payload_helper_is_provider_specific() -> None:
    spec = _host_port_spec()

    assert build_chat_response_format(spec) == {
        "type": "json_schema",
        "json_schema": {
            "name": "host_port",
            "strict": True,
            "schema": spec.schema,
        },
    }


def test_openai_responses_payload_helper_is_provider_specific() -> None:
    spec = _host_port_spec()

    assert build_responses_text_format(spec) == {
        "type": "json_schema",
        "name": "host_port",
        "strict": True,
        "schema": spec.schema,
    }


def test_openai_strict_schema_validation_rejects_missing_required_key() -> None:
    invalid = StructuredOutputSpec(
        name="invalid",
        schema={
            "type": "object",
            "properties": {"host": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
    )

    with pytest.raises(StructuredOutputSchemaError) as exc_info:
        validate_openai_strict_schema(invalid)

    assert exc_info.value.reason == "missing_required_properties"
