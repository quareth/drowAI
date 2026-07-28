"""Regression tests for ffuf semantic evidence and prompt consumption."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

from agent.context.tool_processor import UniversalToolProcessor
from agent.semantic.enrichment import validate_semantic_evidence_entries
from runtime_shared.semantic.pentest_facts import SemanticEvidenceType
from agent.tool_runtime.result_enrichment import merge_semantic_emitter_metadata
from agent.tools.information_gathering.web_enumeration.contracts import HttpRequestArgs
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.web_applications._ffuf_semantics import (
    build_ffuf_semantic_evidence,
    build_ffuf_semantic_observations,
    detect_ffuf_variant,
)
from agent.tools.web_applications.web_application_fuzzers.ffuf import (
    FfufArgs as FuzzerArgs,
    FfufTool as FuzzerTool,
)
from agent.tools.web_applications.web_crawlers.ffuf import (
    FfufArgs as CrawlerArgs,
    FfufTool as CrawlerTool,
)
from runtime_shared.semantic.pentest_facts import SemanticFactEnvelope, compile_facts

class _EvidenceAwarePromptLLM:
    """Deterministic fake LLM that checks evidence presence in prompt bytes."""

    model = "test-model"

    def __init__(self) -> None:
        self.last_prompt: str | None = None

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs):
        _ = system_prompt, kwargs
        self.last_prompt = user_prompt
        has_autocal = '"name":"autocalibration","type":"baseline","value":true' in user_prompt
        has_filter_size = '"name":"filter_size","type":"baseline","value":"208"' in user_prompt
        has_calibrated_filter_group = '"name":"calibrated_filter_group","type":"matcher_or_filter"' in user_prompt
        has_user_matchers = '"source":"args"' in user_prompt
        has_calibration_filters = '"source":"calibration"' in user_prompt

        missing = [
            label
            for label, present in (
                ("autocalibration", has_autocal),
                ("filter_size", has_filter_size),
                ("calibrated_filter_group", has_calibrated_filter_group),
                ("source=args", has_user_matchers),
                ("source=calibration", has_calibration_filters),
            )
            if not present
        ]

        summary = "evidence-aware ffuf compression"
        if missing:
            summary = f"wrong matchers inferred; forced HTTPS inferred; missing={','.join(missing)}"

        return SimpleNamespace(
            content="",
            structured_output={
                "summary": summary,
                "key_findings": [
                    "ffuf empty results observed",
                    "autocalibration baseline consumed",
                ],
                "structured_signals": [],
                "decision_evidence": [],
                "lossiness_risk": "low",
            },
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def _ffuf_args_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        target="https://example.com/FUZZ",
        threads=40,
        request_timeout=20,
        method="GET",
        wordlist=None,
        wordlists=None,
        inline_wordlist=None,
        input_cmd=None,
        match_status="200,201,301",
        match_lines="10-100",
        match_words="30-300",
        match_size="100-5000",
        match_time="0-3000",
        match_regex="admin|api|login",
        filter_status="404,500",
        filter_lines="0-5",
        filter_words="0-20",
        filter_size="0-200",
        filter_time="0-200",
        filter_regex="health|metrics",
        auto_calibrate=True,
        auto_calibrate_strategies=["basic"],
        stop_on_403=True,
        stop_on_errors=True,
        stop_on_any=True,
    )


def test_ffuf_variant_detection() -> None:
    explicit = {"ffuf_variant": "  Fuzzer  ", "config": {"url": "https://example.com/FUZZ"}}
    inferred_crawler = {"config": {"url": "https://example.com/FUZZ"}}
    inferred_crawler_from_flags = {"commandline": ["ffuf", "-u", "https://example.com/X", "-recursion"]}
    inferred_fuzzer = {"config": {"url": "https://example.com/search?q=FUZZ"}}

    assert detect_ffuf_variant(explicit) == "fuzzer"
    assert detect_ffuf_variant(inferred_crawler) == "crawler"
    assert detect_ffuf_variant(inferred_crawler_from_flags) == "crawler"
    assert detect_ffuf_variant(inferred_fuzzer) == "fuzzer"


def test_ffuf_empty_results_plus_autocalibration_evidence() -> None:
    args = _ffuf_args_namespace()
    metadata = {
        "ffuf_variant": "fuzzer",
        "config": {
            "url": "https://example.com/FUZZ",
            "matchers": {
                "IsCalibrated": True,
                "Filters": {
                    "size": 208,
                },
            },
        },
        "results": [],
    }

    evidence = build_ffuf_semantic_evidence(metadata, args)
    valid, dropped = validate_semantic_evidence_entries(evidence)

    assert dropped == []
    baseline_entries = [entry for entry in valid if entry["type"] == SemanticEvidenceType.BASELINE.value]
    matcher_entries = [entry for entry in valid if entry["type"] == SemanticEvidenceType.MATCHER_OR_FILTER.value]

    assert any(
        entry["name"] == "autocalibration" and entry["value"] is True
        for entry in baseline_entries
    )
    assert any(
        entry["name"] == "filter_size" and str(entry["value"]) == "208"
        for entry in baseline_entries
    )
    assert any(
        entry["name"] == "calibrated_filter_group"
        and entry.get("detail", {}).get("source") == "calibration"
        for entry in matcher_entries
    )
    assert any(
        entry.get("detail", {}).get("source") == "args"
        for entry in matcher_entries
    )


def test_ffuf_crawler_emits_path_discovered_when_results_present() -> None:
    tool = CrawlerTool()
    args = CrawlerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"])
    metadata = {
        "config": {"url": "https://example.com/FUZZ"},
        "results": [
            {"url": "https://example.com/admin", "status": 200, "length": 321},
        ],
    }

    observations = tool.emit_semantic_observations(
        stdout="",
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation["observation_type"] == "web.path_discovered"
    assert observation["subject_key"] == "web.path:https://example.com/admin"
    assert observation["payload"]["path"] == "/admin"


def test_ffuf_ip_response_emits_complete_web_surface_fact_set() -> None:
    observations = build_ffuf_semantic_observations(
        {
            "ffuf_variant": "crawler",
            "config": {"url": "http://198.51.100.24/FUZZ"},
            "results": [
                {"url": "http://198.51.100.24/ip", "status": 200, "length": 17455},
                {"url": "http://198.51.100.24/capture", "status": 302, "length": 220},
            ],
        },
        CrawlerArgs(
            target="http://198.51.100.24/FUZZ",
            inline_wordlist=["ip", "capture"],
        ),
    )

    identities = [
        (
            observation["observation_type"],
            observation["subject_type"],
            observation["subject_key"],
        )
        for observation in observations
    ]
    assert identities == [
        ("network.host_discovered", "host.ip", "host.ip:198.51.100.24"),
        (
            "network.service_observed",
            "service.socket",
            "service.socket:198.51.100.24/tcp/80",
        ),
        (
            "web.path_discovered",
            "web.path",
            "web.path:http://198.51.100.24/ip",
        ),
        (
            "web.path_discovered",
            "web.path",
            "web.path:http://198.51.100.24/capture",
        ),
    ]
    service_payload = observations[1]["payload"]
    assert service_payload["service_name"] == "http"
    assert service_payload["application_protocol"] == "http"


def test_http_request_and_ffuf_emit_same_shared_web_response_facts() -> None:
    url = "http://198.51.100.10/download/1"
    ffuf_observations = build_ffuf_semantic_observations(
        {
            "ffuf_variant": "crawler",
            "config": {"url": "http://198.51.100.10/download/FUZZ"},
            "results": [{"url": url, "status": 200, "length": 24}],
        },
        CrawlerArgs(
            target="http://198.51.100.10/download/FUZZ",
            inline_wordlist=["1"],
        ),
    )

    http_args = HttpRequestArgs(target=url, method="GET")
    http_stdout = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/vnd.tcpdump.pcap\r\n"
        "Content-Length: 24\r\n\r\n"
        "mock-body\n"
        "__DROWAI_HTTP_META__200\thttp://198.51.100.10/download/1\t"
        "application/vnd.tcpdump.pcap\t24\t0\t0.125"
    )
    http_tool = HttpRequestTool()
    enriched_http_metadata = merge_semantic_emitter_metadata(
        tool=http_tool,
        args=http_args,
        stdout=http_stdout,
        stderr="",
        exit_code=0,
        existing_metadata={},
    )
    http_observations = enriched_http_metadata["semantic_observations"]

    ignored_payload_keys = {"source", "target_url", "evidence_source"}

    def shared_domain_view(
        observations: list[dict[str, object]],
    ) -> list[tuple[object, ...]]:
        return sorted(
            (
                observation["observation_type"],
                observation["subject_type"],
                observation["subject_key"],
                tuple(
                    sorted(
                        (key, str(value))
                        for key, value in observation["payload"].items()
                        if key not in ignored_payload_keys
                    )
                ),
            )
            for observation in observations
        )

    assert shared_domain_view(http_observations) == shared_domain_view(ffuf_observations)


def test_http_request_without_confirmed_response_emits_no_web_facts() -> None:
    args = HttpRequestArgs(target="http://198.51.100.10/download/1", method="GET")

    assert HttpRequestTool().emit_semantic_observations(
        "",
        "connection refused",
        7,
        args,
        {
            "effective_url": args.target,
            "status_code": None,
            "content_length": None,
        },
    ) == []


def test_ffuf_variants_emit_same_web_path_contract_for_equivalent_results() -> None:
    result_row = {
        "url": "HTTPS://Example.com:443//admin?debug=1#fragment",
        "status": 301,
        "length": 512,
        "words": 22,
        "lines": 7,
        "redirectlocation": "https://example.com/login",
        "content-type": "text/html; charset=utf-8",
    }
    crawler_metadata = {
        "ffuf_variant": "crawler",
        "config": {"url": "https://example.com/FUZZ"},
        "results": [result_row],
    }
    fuzzer_metadata = {
        "ffuf_variant": "fuzzer",
        "config": {"url": "https://example.com/FUZZ"},
        "results": [result_row],
    }

    crawler_observations = build_ffuf_semantic_observations(
        crawler_metadata,
        CrawlerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"]),
    )
    fuzzer_observations = build_ffuf_semantic_observations(
        fuzzer_metadata,
        FuzzerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"]),
    )

    assert len(crawler_observations) == 1
    assert len(fuzzer_observations) == 1
    crawler_payload = dict(crawler_observations[0]["payload"])
    fuzzer_payload = dict(fuzzer_observations[0]["payload"])
    assert crawler_payload.pop("source") == "web_applications.web_crawlers.ffuf"
    assert fuzzer_payload.pop("source") == "web_applications.web_application_fuzzers.ffuf"
    assert crawler_payload == fuzzer_payload == {
        "url": "https://example.com/admin",
        "path": "/admin",
        "target_url": "https://example.com/FUZZ",
        "status_code": 301,
        "response_size": 512,
    }
    assert crawler_observations[0]["subject_key"] == "web.path:https://example.com/admin"
    assert fuzzer_observations[0]["subject_key"] == "web.path:https://example.com/admin"


def test_ffuf_fuzzer_emits_web_path_discovered_when_results_present() -> None:
    tool = FuzzerTool()
    args = FuzzerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"])
    metadata = {
        "config": {"url": "https://example.com/FUZZ"},
        "results": [
            {"url": "https://example.com/admin", "status": 200, "length": 321},
        ],
    }

    observations = tool.emit_semantic_observations(
        stdout="",
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )

    assert len(observations) == 1
    assert observations[0]["payload"]["source"] == "web_applications.web_application_fuzzers.ffuf"


def test_ffuf_fuzzer_empty_run_emits_no_observations() -> None:
    tool = FuzzerTool()
    args = FuzzerArgs(
        target="https://example.com/FUZZ",
        inline_wordlist=["admin"],
    )
    metadata = {
        "config": {"url": "https://example.com/FUZZ"},
        "results": [],
    }

    observations = tool.emit_semantic_observations(
        stdout="",
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )

    assert observations == []


def test_ffuf_semantic_observations_match_filter_cap_duplicate_and_calibration_behavior() -> None:
    metadata = {
        "ffuf_variant": "crawler",
        "config": {
            "url": "https://example.com/FUZZ",
            "matchers": {
                "IsCalibrated": True,
                "Filters": {"status": "200", "size": "123", "words": "9", "lines": "3"},
            },
        },
        "results": [
            {"url": "https://example.com/soft-404", "status": 404, "length": 120},
            {"url": "https://example.com/calibrated", "status": 200, "length": 123, "words": 9, "lines": 3},
            {"url": "https://example.com/redirect", "status": 302, "length": 512, "redirect": "/login"},
            {"url": "https://example.com/dup", "status": 403, "length": 42},
            {"url": "https://example.com/dup", "status": 403, "length": 42},
        ],
    }

    observations = build_ffuf_semantic_observations(
        metadata,
        CrawlerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"]),
    )
    subject_keys = [item["subject_key"] for item in observations]
    payloads = {item["subject_key"]: item["payload"] for item in observations}

    assert "web.path:https://example.com/soft-404" not in subject_keys
    assert subject_keys.count("web.path:https://example.com/dup") == 1
    assert payloads["web.path:https://example.com/calibrated"]["calibrated"] is True
    assert "calibration_filters" not in payloads["web.path:https://example.com/calibrated"]
    assert "redirect_url" not in payloads["web.path:https://example.com/redirect"]


def test_ffuf_semantic_observations_respect_per_origin_cap_order() -> None:
    results = [{"url": "https://example.com/important", "status": 200, "length": 1}]
    results.extend(
        {"url": f"https://example.com/p{index:03d}", "status": 500, "length": index}
        for index in range(205)
    )
    observations = build_ffuf_semantic_observations(
        {
            "ffuf_variant": "fuzzer",
            "config": {"url": "https://example.com/FUZZ"},
            "results": results,
        },
        FuzzerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"]),
    )
    subject_keys = {item["subject_key"] for item in observations}

    assert len(observations) == 200
    assert "web.path:https://example.com/important" in subject_keys
    assert "web.path:https://example.com/p204" not in subject_keys


def test_ffuf_semantic_observations_compile_directly_as_supported_web_path_facts() -> None:
    observations = build_ffuf_semantic_observations(
        {
            "ffuf_variant": "crawler",
            "config": {"url": "https://example.com/FUZZ"},
            "results": [
                {
                    "url": "https://example.com/admin",
                    "status": 200,
                    "length": 512,
                    "words": 22,
                    "lines": 7,
                    "redirectlocation": "https://example.com/login",
                    "content-type": "text/html",
                }
            ],
        },
        CrawlerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"]),
    )
    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version="ffuf.v1",
            capability_family="web_discovery",
            observations=tuple(observations),
            evidence=(),
        )
    )

    assert compiled.accepted_count == 1
    assert compiled.rejected_count == 0
    assert compiled.diagnostics == ()
    assert compiled.facts[0].payload == {
        "source": "web_applications.web_crawlers.ffuf",
        "path": "/admin",
        "target_url": "https://example.com/FUZZ",
        "status_code": 200,
        "response_size": 512,
    }


def test_ffuf_prompt_regression_from_design_doc() -> None:
    """End-to-end regression: raw ffuf JSON stdout → real emitter/merge → prompt.

    Exercises the Phase 6 wiring seam (``FuzzerTool.emit_semantic_*`` via
    ``merge_semantic_emitter_metadata``) so a regression that silently breaks
    the emitter path would fail this test, not just a compressor-consumption
    check built on pre-injected semantic data.
    """
    llm = _EvidenceAwarePromptLLM()
    processor = UniversalToolProcessor(
        llm_client=llm,
        logger=logging.getLogger("test.ffuf.prompt.regression"),
    )
    processor._LLM_BYPASS_MAX_CHARS = 0
    processor._LLM_BYPASS_MAX_LINES = 0
    tool = FuzzerTool()
    args = FuzzerArgs(
        target="http://10.10.11.242/FUZZ",
        inline_wordlist=["admin", "login"],
        match_status="200,201,301",
        match_lines="10-100",
        match_words="30-300",
        match_size="100-5000",
        match_time="0-3000",
        match_regex="admin|api|login",
        filter_status="404,500",
        filter_lines="0-5",
        filter_words="0-20",
        filter_size="0-200",
        filter_time="0-200",
        filter_regex="health|metrics",
        auto_calibrate=True,
        auto_calibrate_strategies=["basic"],
        stop_on_403=True,
        stop_on_errors=True,
        stop_on_any=True,
    )
    raw_ffuf_json_stdout = json.dumps(
        {
            "commandline": ["ffuf", "-u", "http://10.10.11.242/FUZZ"],
            "time": {"start": "2026-04-21T00:00:00Z", "end": "2026-04-21T00:00:10Z"},
            "config": {
                "url": "http://10.10.11.242/FUZZ",
                "matchers": {
                    "IsCalibrated": True,
                    "Filters": {"size": 208},
                },
            },
            "results": [],
        }
    )

    merged_metadata = merge_semantic_emitter_metadata(
        tool=tool,
        args=args,
        stdout=raw_ffuf_json_stdout,
        stderr="",
        exit_code=0,
        existing_metadata=None,
    )

    wrapped_metadata = {"tool_metadata": merged_metadata}
    result = asyncio.run(
        processor.process_output(
            "web_applications.web_application_fuzzers.ffuf",
            raw_ffuf_json_stdout,
            metadata=wrapped_metadata,
        )
    )

    assert llm.last_prompt is not None
    assert '"baseline":[{' in llm.last_prompt
    assert '"name":"autocalibration","type":"baseline","value":true' in llm.last_prompt
    assert '"name":"filter_size","type":"baseline","value":"208"' in llm.last_prompt
    assert '"matcher_or_filter":[' in llm.last_prompt
    assert '"name":"calibrated_filter_group","type":"matcher_or_filter"' in llm.last_prompt
    assert '"source":"args"' in llm.last_prompt
    assert '"source":"calibration"' in llm.last_prompt
    assert '"name":"ffuf_variant","type":"variant","value":"fuzzer"' in llm.last_prompt

    assert "wrong matchers" not in result.summary.lower()
    assert "forced https" not in result.summary.lower()


def test_ffuf_result_summary_has_no_duplicate_counts() -> None:
    args = _ffuf_args_namespace()
    metadata = {
        "ffuf_variant": "fuzzer",
        "config": {"url": "https://example.com/FUZZ"},
        "results": [
            {"url": "https://example.com/admin", "status": 200},
            {"url": "https://example.com/login", "status": 302},
        ],
    }

    evidence = build_ffuf_semantic_evidence(metadata, args)
    valid, dropped = validate_semantic_evidence_entries(evidence)

    assert dropped == []
    result_summary_names = [
        entry["name"]
        for entry in valid
        if entry["type"] == SemanticEvidenceType.RESULT_SUMMARY.value
    ]
    assert result_summary_names.count("results_count") == 1
    assert "results_count_after_filters" not in result_summary_names


def test_ffuf_prompt_variant_matches_tool_class_for_fuzzer() -> None:
    tool = FuzzerTool()
    args = FuzzerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"])
    metadata = {
        "config": {"url": "https://example.com/FUZZ"},
        "results": [],
    }

    evidence = tool.emit_semantic_evidence(
        stdout="",
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )
    variant_entry = next(entry for entry in evidence if entry.get("type") == "variant")

    assert variant_entry["name"] == "ffuf_variant"
    assert variant_entry["value"] == "fuzzer"


def test_ffuf_prompt_variant_matches_tool_class_for_crawler() -> None:
    tool = CrawlerTool()
    args = CrawlerArgs(target="https://example.com/FUZZ", inline_wordlist=["admin"])
    metadata = {
        "config": {"url": "https://example.com/FUZZ"},
        "results": [],
    }

    evidence = tool.emit_semantic_evidence(
        stdout="",
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )
    variant_entry = next(entry for entry in evidence if entry.get("type") == "variant")

    assert variant_entry["name"] == "ffuf_variant"
    assert variant_entry["value"] == "crawler"


def test_ffuf_semantic_evidence_smoke_respects_per_type_caps() -> None:
    args = _ffuf_args_namespace()
    metadata = {
        "ffuf_variant": "crawler",
        "config": {
            "url": "https://example.com/FUZZ",
            "matchers": {
                "IsCalibrated": True,
                "filters": {
                    "status": "403,429",
                    "size": "120,130",
                    "lines": "4-9",
                    "words": "20-40",
                    "time": "0-1000",
                    "regex": "robots|sitemap",
                },
            },
        },
        "results": [{"url": "https://example.com/admin"}],
        "timeout": {"hit": True},
    }

    evidence = build_ffuf_semantic_evidence(metadata, args)
    valid, dropped = validate_semantic_evidence_entries(evidence)

    assert dropped == []
    matcher_entries = [entry for entry in valid if entry["type"] == SemanticEvidenceType.MATCHER_OR_FILTER.value]
    diagnostic_entries = [entry for entry in valid if entry["type"] == SemanticEvidenceType.DIAGNOSTIC.value]
    assert len(matcher_entries) == 5
    assert len(diagnostic_entries) == 3
    assert {entry["name"] for entry in diagnostic_entries} == {
        "stop_flags_active",
        "wordlist_missing",
        "timeout_hit",
    }
