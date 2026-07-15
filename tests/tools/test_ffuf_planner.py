"""Planner-contract tests for ffuf crawler and fuzzer tools."""

from __future__ import annotations

import pytest

from agent.tools.parameter_validation import validate_tool_parameters


def test_ffuf_crawler_planner_compiles_semantic_path_catalog() -> None:
    result = validate_tool_parameters(
        "web_applications.web_crawlers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "https://example.com/FUZZ",
            "payload_source": {"kind": "catalog", "family": "paths", "profile": "small"},
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["target"] == "https://example.com/FUZZ"
    assert result.normalized_parameters["wordlist"] == "/usr/share/seclists/Discovery/Web-Content/common.txt"


def test_ffuf_crawler_planner_compiles_recursive_path_scan() -> None:
    result = validate_tool_parameters(
        "web_applications.web_crawlers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "https://example.com/FUZZ",
            "payload_source": {"kind": "catalog", "family": "paths", "profile": "medium"},
            "recursion": {"enabled": True, "depth": 2, "strategy": "greedy"},
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["recursion"] is True
    assert result.normalized_parameters["recursion_depth"] == 2
    assert result.normalized_parameters["recursion_strategy"] == "greedy"


def test_ffuf_crawler_planner_rejects_output_controls() -> None:
    result = validate_tool_parameters(
        "web_applications.web_crawlers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "https://example.com/FUZZ",
            "payload_source": {"kind": "catalog", "family": "paths", "profile": "small"},
            "advanced": {
                "silent": True,
                "json_output_path": "artifacts/ffuf.json",
            },
        },
        validation_stage="planner",
    )

    assert result.valid is False
    assert result.reason == "schema_validation_error"


def test_ffuf_fuzzer_planner_compiles_header_vhost_fuzzing() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "header",
            "target_template": "https://example.com/",
            "payload_source": {"kind": "catalog", "family": "vhosts", "profile": "small"},
            "request_shape": {"header_templates": ["Host: FUZZ.example.com"]},
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["headers"] == ["Host: FUZZ.example.com"]
    assert result.normalized_parameters["wordlist"] == "/usr/share/seclists/Discovery/DNS/subdomains-top1million-5000.txt"


def test_ffuf_fuzzer_planner_compiles_query_combo_fuzzing() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "query",
            "target_template": "https://example.com/api?PARAM=VALUE",
            "payload_source": {
                "kind": "catalog_combo",
                "combo_mode": "pitchfork",
                "items": [
                    {"family": "parameter_names", "keyword": "PARAM", "profile": "small"},
                    {"family": "common_values", "keyword": "VALUE", "profile": "small"},
                ],
            },
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["combo_mode"] == "pitchfork"
    assert result.normalized_parameters["wordlists"] == [
        {
            "path": "/usr/share/seclists/Discovery/Web-Content/burp-parameter-names.txt",
            "keyword": "PARAM",
        },
        {
            "path": "/usr/share/seclists/Fuzzing/interesting-values.txt",
            "keyword": "VALUE",
        },
    ]


def test_ffuf_fuzzer_planner_compiles_cookie_fuzzing_with_inline_values() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "cookie",
            "target_template": "https://example.com/profile",
            "payload_source": {"kind": "inline_values", "values": ["a", "b", "c"]},
            "request_shape": {"cookie_template": "session=FUZZ"},
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["cookies"] == "session=FUZZ"
    assert result.normalized_parameters["inline_wordlist"] == ["a", "b", "c"]


def test_ffuf_fuzzer_planner_compiles_large_generated_sequence_to_inline_values() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "https://example.com/data/FUZZ",
            "payload_source": {"kind": "generated_sequence", "start": 1, "end": 500},
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert "input_cmd" not in result.normalized_parameters
    assert len(result.normalized_parameters["inline_wordlist"]) == 500
    assert result.normalized_parameters["inline_wordlist"][:3] == ["1", "2", "3"]
    assert result.normalized_parameters["inline_wordlist"][-1] == "500"


def test_ffuf_fuzzer_planner_compiles_body_fuzzing() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "body",
            "target_template": "https://example.com/api",
            "payload_source": {"kind": "catalog", "family": "common_values", "profile": "small"},
            "request_shape": {
                "method": "POST",
                "header_templates": ["Content-Type: application/json"],
                "body_template": '{"value":"FUZZ"}',
            },
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["method"] == "POST"
    assert result.normalized_parameters["data"] == '{"value":"FUZZ"}'
    assert result.normalized_parameters["headers"] == ["Content-Type: application/json"]


def test_ffuf_fuzzer_planner_compiles_raw_request_fuzzing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    request_file = tmp_path / "requests" / "base.txt"
    request_file.parent.mkdir(parents=True, exist_ok=True)
    request_file.write_text(
        "GET /api/FUZZ HTTP/1.1\nHost: example.com\nUser-Agent: ffuf\n\n",
        encoding="utf-8",
    )

    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "raw_request",
            "target_template": "https://example.com/",
            "payload_source": {"kind": "generated_sequence", "start": 1, "end": 3},
            "request_shape": {"raw_request_file": "requests/base.txt", "request_proto": "https"},
        },
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters["raw_request_file"] == "requests/base.txt"
    assert result.normalized_parameters["request_proto"] == "https"
    assert result.normalized_parameters["inline_wordlist"] == ["1", "2", "3"]


def test_ffuf_fuzzer_planner_rejects_mismatched_surface_and_template() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "header",
            "target_template": "https://example.com/FUZZ",
            "payload_source": {"kind": "catalog", "family": "vhosts", "profile": "small"},
            "request_shape": {"header_templates": ["User-Agent: test"]},
        },
        validation_stage="planner",
    )

    assert result.valid is False
    assert result.reason == "semantic_validation_error"
    assert any("declared fuzz_surface 'header'" in err["message"] for err in result.validation_errors)


def test_ffuf_fuzzer_planner_rejects_named_combo_when_keyword_missing() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "query",
            "target_template": "https://example.com/api?PARAM=1",
            "payload_source": {
                "kind": "catalog_combo",
                "items": [
                    {"family": "parameter_names", "keyword": "PARAM", "profile": "small"},
                    {"family": "common_values", "keyword": "VALUE", "profile": "small"},
                ],
            },
        },
        validation_stage="planner",
    )

    assert result.valid is False
    assert result.reason == "semantic_validation_error"
    assert any("VALUE" in err["message"] for err in result.validation_errors)


def test_ffuf_fuzzer_planner_rejects_unsupported_custom_wordlist_path() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "https://example.com/FUZZ",
            "payload_source": {"kind": "custom_wordlist", "path": "/tmp/words.txt"},
        },
        validation_stage="planner",
    )

    assert result.valid is False
    assert result.reason == "semantic_validation_error"
    assert any("Absolute wordlist paths" in err["message"] for err in result.validation_errors)


def test_ffuf_fuzzer_planner_rejects_execution_only_recursion_field() -> None:
    result = validate_tool_parameters(
        "web_applications.web_application_fuzzers.ffuf",
        {
            "fuzz_surface": "path",
            "target_template": "https://example.com/FUZZ",
            "payload_source": {"kind": "catalog", "family": "paths", "profile": "small"},
            "recursion": {"enabled": True},
        },
        validation_stage="planner",
    )

    assert result.valid is False
    assert result.reason == "schema_validation_error"
    assert any("extra" in err["message"].lower() or "recursion" in err["field"] for err in result.validation_errors)
