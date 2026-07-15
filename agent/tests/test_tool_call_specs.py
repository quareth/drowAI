"""Tests for neutral and OpenAI-compatible planner tool spec builders."""

import os
import sys
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO_ROOT)

from agent.tools.builder_intent import BUILDER_INTENT_KEY
from agent.tools.tool_call_specs import (
    build_function_tool_spec_for,
    build_function_tool_specs_for,
    build_openai_tool_spec_for,
    build_openai_tool_specs_for,
    make_function_name_for_tool,
)
from agent.tools.tool_registry import available_tools, get_tool_metadata


def test_make_function_name_is_deterministic():
    fn = make_function_name_for_tool("information_gathering.network_discovery.nmap")
    assert fn == "tool__information_gathering_network_discovery_nmap"


@pytest.mark.skipif(
    "information_gathering.network_discovery.nmap" not in available_tools(),
    reason="nmap tool not available in this environment",
)
def test_build_single_spec_contains_schema():
    tool_id = "information_gathering.network_discovery.nmap"
    spec = build_openai_tool_spec_for(tool_id)
    assert spec["type"] == "function"
    assert spec["function"]["name"] == make_function_name_for_tool(tool_id)
    params = spec["function"].get("parameters", {})
    assert isinstance(params, dict)
    assert params.get("type") == "object"
    assert "properties" in params


@pytest.mark.skipif(
    "information_gathering.network_discovery.nmap" not in available_tools(),
    reason="nmap tool not available in this environment",
)
def test_build_single_neutral_spec_contains_tool_identity_and_schema():
    tool_id = "information_gathering.network_discovery.nmap"
    spec = build_function_tool_spec_for(tool_id)

    assert spec.tool_id == tool_id
    assert spec.name == make_function_name_for_tool(tool_id)
    assert spec.parameters_schema.get("type") == "object"
    assert "properties" in spec.parameters_schema


@pytest.mark.skipif(
    "web_applications.web_application_fuzzers.ffuf" not in available_tools(),
    reason="ffuf fuzzer tool not available in this environment",
)
def test_ffuf_spec_uses_planner_schema_and_guidance():
    tool_id = "web_applications.web_application_fuzzers.ffuf"
    spec = build_openai_tool_spec_for(tool_id)

    params = spec["function"]["parameters"]
    assert "target_template" in params.get("properties", {})
    assert "payload_source" in params.get("properties", {})
    assert "wordlist" not in params.get("properties", {})
    assert "Planner Guidance:" in spec["function"]["description"]


@pytest.mark.skipif(
    "information_gathering.network_discovery.nmap" not in available_tools(),
    reason="nmap tool not available in this environment",
)
def test_spec_injects_reserved_builder_intent_property():
    tool_id = "information_gathering.network_discovery.nmap"
    spec = build_function_tool_spec_for(tool_id)

    props = spec.parameters_schema.get("properties", {})
    assert BUILDER_INTENT_KEY in props
    assert props[BUILDER_INTENT_KEY]["type"] == "string"
    required = spec.parameters_schema.get("required")
    if isinstance(required, list):
        assert BUILDER_INTENT_KEY in required


def test_build_multiple_specs_mapping_roundtrip():
    # Use a small subset of valid executable tools.
    tools = []
    for tool_id in available_tools():
        try:
            get_tool_metadata(tool_id)
        except Exception:
            continue
        tools.append(tool_id)
    subset = tools[:2] if tools else []
    specs, mapping = build_openai_tool_specs_for(subset)
    assert len(specs) == len(subset)
    for spec in specs:
        name = spec["function"]["name"]
        assert name in mapping
        assert mapping[name] in subset


def test_build_neutral_specs_mapping_roundtrip():
    # Use a small subset of valid executable tools.
    tools = []
    for tool_id in available_tools():
        try:
            get_tool_metadata(tool_id)
        except Exception:
            continue
        tools.append(tool_id)
    subset = tools[:2] if tools else []
    specs, mapping = build_function_tool_specs_for(subset)

    assert len(specs) == len(subset)
    for spec in specs:
        assert spec.name in mapping
        assert mapping[spec.name] == spec.tool_id


@pytest.mark.skipif(
    "information_gathering.network_discovery.nmap" not in available_tools(),
    reason="nmap tool not available in this environment",
)
def test_build_specs_deduplicates_duplicate_tool_ids():
    tool_id = "information_gathering.network_discovery.nmap"
    specs, mapping = build_openai_tool_specs_for([tool_id, tool_id])
    assert len(specs) == 1
    assert mapping == {make_function_name_for_tool(tool_id): tool_id}


@pytest.mark.skipif(
    "information_gathering.network_discovery.nmap" not in available_tools(),
    reason="nmap tool not available in this environment",
)
def test_build_neutral_specs_deduplicates_duplicate_tool_ids():
    tool_id = "information_gathering.network_discovery.nmap"
    specs, mapping = build_function_tool_specs_for([tool_id, tool_id])
    assert len(specs) == 1
    assert specs[0].tool_id == tool_id
    assert mapping == {make_function_name_for_tool(tool_id): tool_id}
