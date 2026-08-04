"""Regression tests for producer-owned Gobuster web-path semantics.

These tests lock the Gobuster semantic emitter over parsed metadata only. They
intentionally avoid command execution, raw text parsing, artifact reads, and
backend adapter behavior.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.tools.web_applications.web_crawlers.gobuster import (
    GobusterArgs,
    GobusterTool,
    build_gobuster_semantic_observations,
)
from runtime_shared.semantic.pentest_facts import SemanticFactEnvelope, compile_facts


def test_gobuster_dir_metadata_emits_canonical_web_path_payload() -> None:
    metadata = {
        "findings": [
            {
                "path": "/admin",
                "status": 301,
                "size": 316,
                "redirect_url": "https://example.com/admin/",
            }
        ],
    }
    args = GobusterArgs(target="HTTPS://Example.com:443/base/../", wordlist="list.txt")

    observations = build_gobuster_semantic_observations(metadata, args)

    assert observations == [
        {
            "observation_type": "web.path_discovered",
            "subject_type": "web.path",
            "subject_key": "web.path:https://example.com/admin",
            "payload": {
                "url": "https://example.com/admin",
                "source": "web_applications.web_crawlers.gobuster",
                "path": "/admin",
                "target_url": "https://example.com/base/../",
                "status_code": 301,
                "response_size": 316,
            },
        }
    ]


def test_gobuster_tool_emits_from_parsed_metadata_without_raw_output() -> None:
    tool = GobusterTool()
    args = GobusterArgs(target="https://example.com", wordlist="list.txt")

    observations = tool.emit_semantic_observations(
        stdout="/ignored (Status: 200) [Size: 1]",
        stderr="",
        exit_code=0,
        args=args,
        metadata={"findings": [{"path": "/metadata", "status": 200, "size": 42}]},
    )

    assert observations[0]["subject_key"] == "web.path:https://example.com/metadata"
    assert observations[0]["payload"]["response_size"] == 42


def test_gobuster_vhost_mode_uses_same_supported_http_contract() -> None:
    args = GobusterArgs(target="https://example.com", wordlist="list.txt", mode="vhost")

    observations = build_gobuster_semantic_observations(
        {"findings": [{"path": "/admin", "status": 200, "size": 512}]},
        args,
    )

    assert observations[0]["subject_key"] == "web.path:https://example.com/admin"
    assert observations[0]["payload"]["source"] == "web_applications.web_crawlers.gobuster"


def test_gobuster_dns_mode_never_emits_web_paths() -> None:
    args = GobusterArgs(target="example.com", wordlist="list.txt", mode="dns")

    observations = build_gobuster_semantic_observations(
        {"findings": [{"path": "/admin", "status": 200, "size": 512}]},
        args,
    )

    assert observations == []


def test_gobuster_malformed_missing_target_and_empty_inputs_emit_no_invalid_facts() -> None:
    missing_target_args = SimpleNamespace(mode="dir", target="")
    valid_args = GobusterArgs(target="https://example.com", wordlist="list.txt")

    assert build_gobuster_semantic_observations({"findings": []}, valid_args) == []
    assert build_gobuster_semantic_observations(
        {"findings": [{"path": "/admin", "status": 200}]},
        missing_target_args,
    ) == []
    assert build_gobuster_semantic_observations(
        {"findings": [{"path": "admin", "status": 200}, {"status": 200}, "bad"]},
        valid_args,
    ) == []


def test_gobuster_found_paths_fallback_matches_supported_metadata_behavior() -> None:
    args = GobusterArgs(target="https://example.com", wordlist="list.txt")

    observations = build_gobuster_semantic_observations(
        {
            "findings": [{"status": 200}],
            "found_paths": ["/admin"],
        },
        args,
    )

    assert observations[0]["subject_key"] == "web.path:https://example.com/admin"
    assert observations[0]["payload"] == {
        "url": "https://example.com/admin",
        "source": "web_applications.web_crawlers.gobuster",
        "path": "/admin",
        "target_url": "https://example.com/",
    }


def test_gobuster_semantic_order_and_dedupe_match_adapter_policy_without_cap() -> None:
    args = GobusterArgs(target="https://example.com", wordlist="list.txt")
    findings = [{"path": "/important", "status": 200, "size": 1}]
    findings.extend({"path": f"/p{index:03d}", "status": 500, "size": index} for index in range(205))
    findings.extend(
        [
            {"path": "/duplicate", "status": 403, "size": 42},
            {"path": "/duplicate", "status": 403, "size": 42},
        ]
    )

    observations = build_gobuster_semantic_observations({"findings": findings}, args)
    subject_keys = {item["subject_key"] for item in observations}

    assert len(observations) == 207
    assert observations[0]["subject_key"] == "web.path:https://example.com/important"
    assert "web.path:https://example.com/p204" in subject_keys
    assert sum(1 for item in observations if item["subject_key"].endswith("/duplicate")) == 1


def test_gobuster_semantic_observations_compile_directly_as_web_path_facts() -> None:
    args = GobusterArgs(target="https://example.com", wordlist="list.txt")
    observations = build_gobuster_semantic_observations(
        {"findings": [{"path": "/admin", "status": 200, "size": 512}]},
        args,
    )

    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version="gobuster.v1",
            capability_family="web_crawling",
            observations=tuple(observations),
            evidence=(),
        )
    )

    assert compiled.accepted_count == 1
    assert compiled.rejected_count == 0
    assert compiled.diagnostics == ()
