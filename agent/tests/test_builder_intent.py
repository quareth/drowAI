"""Tests for the reserved builder-intent meta-field helpers."""

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from agent.tools.builder_intent import (
    BUILDER_INTENT_KEY,
    inject_builder_intent_property,
    split_builder_intent,
)


def test_inject_adds_property_and_marks_required_when_required_list_present():
    schema = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}
    out = inject_builder_intent_property(schema)
    assert BUILDER_INTENT_KEY in out["properties"]
    assert BUILDER_INTENT_KEY in out["required"]


def test_inject_leaves_optional_when_no_required_list():
    schema = {"type": "object", "properties": {}}
    out = inject_builder_intent_property(schema)
    assert BUILDER_INTENT_KEY in out["properties"]
    assert "required" not in out


def test_inject_is_idempotent_and_noop_on_non_object():
    schema = {"type": "object", "properties": {BUILDER_INTENT_KEY: {"type": "string"}}, "required": []}
    out = inject_builder_intent_property(schema)
    assert out["required"] == []  # not appended twice
    assert inject_builder_intent_property({"$ref": "#/$defs/X"}) == {"$ref": "#/$defs/X"}


def test_split_strips_intent_from_dict():
    params, intent = split_builder_intent({"target": "10.0.0.1", BUILDER_INTENT_KEY: "scan host"})
    assert params == {"target": "10.0.0.1"}
    assert intent == "scan host"


def test_split_strips_intent_from_json_string():
    raw = '{"target": "10.0.0.1", "_builder_intent": "enumerate ports"}'
    params, intent = split_builder_intent(raw)
    assert params == {"target": "10.0.0.1"}
    assert intent == "enumerate ports"


def test_split_preserves_payload_on_decode_failure():
    raw = "{not valid json"
    params, intent = split_builder_intent(raw)
    assert params == raw
    assert intent == ""


def test_split_handles_missing_intent_and_non_object():
    params, intent = split_builder_intent({"target": "x"})
    assert params == {"target": "x"}
    assert intent == ""
    params, intent = split_builder_intent(None)
    assert params is None
    assert intent == ""
