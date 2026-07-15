"""Unit tests for the shared tool-parameter validation layer."""

from __future__ import annotations

from agent.tools.parameter_validation import validate_tool_parameters


def test_container_scoped_tools_reject_direct_transport() -> None:
    result = validate_tool_parameters(
        "shell.exec",
        {"command": "echo test", "transport": "direct"},
    )

    assert result.valid is False
    assert result.reason == "transport_policy_violation"
    assert result.validation_errors[0]["field"] == "transport"
    assert "file-comm" in result.validation_errors[0]["suggested_fix"]


def test_target_autofill_validates_and_normalizes_nmap_parameters() -> None:
    result = validate_tool_parameters(
        "information_gathering.network_discovery.nmap",
        {"scan_types": ["-sV"]},
        action_target="127.0.0.1",
    )

    assert result.valid is True
    assert result.reason is None
    assert result.target_autofill_applied is True
    assert result.provided_parameters == {"scan_types": ["-sV"]}
    assert result.normalized_parameters["target"] == "127.0.0.1"


def test_target_autofill_does_not_force_invalid_ffuf_target() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {"wordlist": "/usr/share/seclists/Discovery/Web-Content/common.txt"},
        action_target="http://10.129.34.54/data/2",
    )

    assert result.valid is False
    assert result.reason == "schema_validation_error"
    assert result.target_autofill_applied is False
    assert any("target" in err["field"] or "target" in err["message"] for err in result.validation_errors)


def test_non_target_tool_keeps_valid_raw_parameters() -> None:
    result = validate_tool_parameters(
        "knowledge.cve_lookup",
        {
            "product": "PostgreSQL",
            "version": "9.6.0",
            "max_results": 5,
        },
        action_target="127.0.0.1",
    )

    assert result.valid is True
    assert result.target_autofill_applied is False
    assert result.normalized_parameters == {
        "product": "PostgreSQL",
        "version": "9.6.0",
        "max_results": 5,
    }


def test_planner_validation_compiles_ffuf_generated_sequence_to_inline_values() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "http://10.129.34.166/data/FUZZ",
            "payload_source": {
                "kind": "generated_sequence",
                "start": 1,
                "end": 3,
            },
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["target"] == "http://10.129.34.166/data/FUZZ"
    assert result.normalized_parameters["inline_wordlist"] == ["1", "2", "3"]
    assert result.provided_parameters["target_template"] == "http://10.129.34.166/data/FUZZ"


def test_planner_validation_compiles_large_generated_sequence_to_inline_values() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "http://10.129.34.166/data/FUZZ",
            "payload_source": {
                "kind": "generated_sequence",
                "start": 1,
                "end": 300,
            },
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert "input_cmd" not in result.normalized_parameters
    assert "input_num" not in result.normalized_parameters
    assert len(result.normalized_parameters["inline_wordlist"]) == 300
    assert result.normalized_parameters["inline_wordlist"][:3] == ["1", "2", "3"]
    assert result.normalized_parameters["inline_wordlist"][-1] == "300"


def test_planner_validation_rejects_placeholder_free_ffuf_target_template() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "http://10.129.34.166/data/2",
            "payload_source": {"kind": "catalog", "family": "paths", "profile": "small"},
        },
        validation_stage="planner",
    )

    assert result.valid is False
    assert result.reason == "semantic_validation_error"
    assert any("fuzz_surface" in err["message"] or "keyword" in err["message"] for err in result.validation_errors)
