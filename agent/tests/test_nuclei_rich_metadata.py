"""Contract tests for nuclei rich metadata extraction.

Validates that the bounded rich metadata contract defined in nuclei_semantics.py
produces deterministic, normalized output from realistic nuclei JSONL fixtures.
Tests cover both new rich fields and preservation of existing legacy fields.
"""

import json
import asyncio
import hashlib
import logging
import copy
from types import SimpleNamespace

import pytest
from agent.semantic.enrichment import validate_semantic_evidence_entries

from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
    parse_nuclei_json,
)
from agent.tools.web_applications.web_vulnerability_scanners.nuclei_semantics import (
    MAX_TAGS,
    MAX_REFERENCES,
    MAX_CVE_IDS,
    MAX_CWE_IDS,
    MAX_EXTRACTED_RESULTS,
    MAX_DESCRIPTION_SUMMARY_LEN,
    MAX_TITLE_LEN,
    NUCLEI_CAPABILITY_FAMILY,
    NUCLEI_SEMANTIC_SCHEMA_VERSION,
    _normalize_classification,
    _normalize_references,
    _normalize_tags,
    _normalize_extracted_results,
    _normalize_severity,
    _normalize_confidence,
    _truncate,
    normalize_result_row,
    build_finding_observation,
    build_nuclei_semantic_evidence,
)


# ---------------------------------------------------------------------------
# Rich JSONL fixture -- realistic but compact nuclei structured output
# ---------------------------------------------------------------------------

RICH_JSONL_ROW = {
    "template-id": "CVE-2024-0001",
    "template": "cves/CVE-2024-0001.yaml",
    "info": {
        "name": "Exposed Default Login Page",
        "severity": "high",
        "description": "Default admin login page exposed to unauthenticated users.",
        "classification": {
            "cve-id": ["CVE-2024-0001"],
            "cwe-id": ["CWE-200"],
        },
        "tags": ["panel", "exposure", "default-login"],
        "reference": [
            "https://vendor.example/advisory",
            "https://cve.example/CVE-2024-0001",
        ],
    },
    "matcher-name": "default-login-page",
    "severity": "high",
    "matched-at": "https://example.com/login",
    "host": "https://example.com",
    "extracted-results": ["admin portal", "password field"],
}

RICH_JSONL_ROW_WITH_CLASSIFICATION = {
    "template-id": "CVE-2023-22515",
    "info": {
        "name": "Confluence Authentication Bypass",
        "severity": "critical",
        "description": "Atlassian Confluence Server allows unauthenticated access to administrative setup endpoints, enabling an attacker to create a new administrator account.",
        "classification": {
            "cve-id": ["CVE-2023-22515"],
            "cwe-id": ["CWE-287", "CWE-863"],
        },
        "tags": ["cve", "confluence", "auth-bypass", "critical"],
        "reference": [
            "https://confluence.atlassian.com/security/cve-2023-22515",
            "https://nvd.nist.gov/vuln/detail/CVE-2023-22515",
            "https://www.rapid7.com/blog/post/2023/10/04/cve-2023-22515-zero-day",
        ],
    },
    "severity": "critical",
    "matched-at": "https://confluence.example.com/setup/setupadministrator.action",
    "host": "https://confluence.example.com",
}

# Minimal row -- only core identity fields, no rich metadata
MINIMAL_JSONL_ROW = {
    "template-id": "tech-detect",
    "severity": "info",
    "matched-at": "http://example.com",
    "matcher-name": "apache",
}

# Row with empty/null optional fields -- tests graceful degradation
PARTIAL_JSONL_ROW = {
    "template-id": "directory-listing",
    "info": {
        "name": "Directory Listing Enabled",
        "severity": "low",
        "description": "",
        "classification": {},
        "tags": [],
        "reference": [],
    },
    "matched-at": "http://example.com/images/",
    "extracted-results": [],
}


def _make_jsonl(*rows: dict) -> str:
    """Build JSONL string from row dicts."""
    return "\n".join(json.dumps(row) for row in rows)


# ---------------------------------------------------------------------------
# Tests: Legacy field preservation (parse_nuclei_json)
# ---------------------------------------------------------------------------

class TestLegacyFieldPreservation:
    """Verify that existing metadata keys from parse_nuclei_json remain unchanged."""

    def test_results_key_present(self):
        metadata = parse_nuclei_json(_make_jsonl(RICH_JSONL_ROW))
        assert "results" in metadata
        assert len(metadata["results"]) == 1

    def test_summary_key_present(self):
        metadata = parse_nuclei_json(_make_jsonl(RICH_JSONL_ROW))
        assert "summary" in metadata
        assert metadata["summary"]["total_results"] == 1

    def test_multiple_rows_parsed(self):
        metadata = parse_nuclei_json(
            _make_jsonl(RICH_JSONL_ROW, MINIMAL_JSONL_ROW, PARTIAL_JSONL_ROW)
        )
        assert len(metadata["results"]) == 3

    def test_raw_row_data_preserved_in_results(self):
        """parse_nuclei_json should preserve the raw row structure in results."""
        metadata = parse_nuclei_json(_make_jsonl(RICH_JSONL_ROW))
        row = metadata["results"][0]
        assert row["template-id"] == "CVE-2024-0001"
        assert row["matched-at"] == "https://example.com/login"
        assert row["severity"] == "high"


# ---------------------------------------------------------------------------
# Tests: Rich result row normalization
# ---------------------------------------------------------------------------

class TestNormalizeResultRow:
    """Verify normalize_result_row() produces the bounded rich shape."""

    def test_core_identity_fields_preserved(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["target_url"] == "https://example.com/login"
        assert result["template_id"] == "CVE-2024-0001"
        assert result["matcher"] == "default-login-page"
        assert result["severity"] == "high"

    def test_title_from_info_name(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["title"] == "Exposed Default Login Page"

    def test_description_summary_from_info(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["description_summary"] == "Default admin login page exposed to unauthenticated users."

    def test_classification_normalized(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["classification"] == {
            "cve_ids": ["CVE-2024-0001"],
            "cwe_ids": ["CWE-200"],
        }

    def test_classification_multiple_cwe(self):
        result = normalize_result_row(RICH_JSONL_ROW_WITH_CLASSIFICATION)
        assert result["classification"]["cwe_ids"] == ["CWE-287", "CWE-863"]

    def test_tags_sorted_and_bounded(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["tags"] == ["default-login", "exposure", "panel"]  # sorted
        assert len(result["tags"]) <= MAX_TAGS

    def test_references_preserved_order(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["references"] == [
            "https://vendor.example/advisory",
            "https://cve.example/CVE-2024-0001",
        ]
        assert len(result["references"]) <= MAX_REFERENCES

    def test_matched_at_preserved(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["matched_at"] == "https://example.com/login"

    def test_extracted_results_bounded(self):
        result = normalize_result_row(RICH_JSONL_ROW)
        assert result["extracted_results"] == ["admin portal", "password field"]
        assert len(result["extracted_results"]) <= MAX_EXTRACTED_RESULTS

    def test_minimal_row_only_core_fields(self):
        result = normalize_result_row(MINIMAL_JSONL_ROW)
        assert result["target_url"] == "http://example.com"
        assert result["template_id"] == "tech-detect"
        assert result["matcher"] == "apache"
        assert result["severity"] == "info"
        # No rich fields on minimal row
        assert "title" not in result
        assert "description_summary" not in result
        assert "classification" not in result
        assert "tags" not in result
        assert "references" not in result
        assert "extracted_results" not in result

    def test_partial_row_graceful_degradation(self):
        """Empty optional fields should not appear in normalized output."""
        result = normalize_result_row(PARTIAL_JSONL_ROW)
        assert result["template_id"] == "directory-listing"
        assert result["title"] == "Directory Listing Enabled"
        assert result["severity"] == "low"
        # Empty description, classification, tags, references, extracted_results should be absent
        assert "description_summary" not in result
        assert "classification" not in result
        assert "tags" not in result
        assert "references" not in result
        assert "extracted_results" not in result

    def test_completely_empty_row(self):
        """An empty dict should produce an empty result."""
        result = normalize_result_row({})
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: Normalization helpers - bounds, ordering, dedup
# ---------------------------------------------------------------------------

class TestNormalizationHelpers:
    """Verify individual normalization functions enforce bounds and ordering."""

    def test_truncate_long_text(self):
        long_text = "x" * 1000
        result = _truncate(long_text, MAX_DESCRIPTION_SUMMARY_LEN)
        assert len(result) == MAX_DESCRIPTION_SUMMARY_LEN
        assert result.endswith("...")

    def test_truncate_short_text_unchanged(self):
        assert _truncate("hello", 256) == "hello"

    def test_truncate_empty(self):
        assert _truncate("") == ""
        assert _truncate(None) == ""

    def test_tags_sorted_deduped_bounded(self):
        tags = ["z-tag", "a-tag", "m-tag", "a-tag", "Z-TAG"] + [f"tag-{i}" for i in range(20)]
        result = _normalize_tags(tags)
        assert result == sorted(result, key=lambda s: s.lower())
        assert len(result) <= MAX_TAGS
        # Dedup is case-insensitive
        assert sum(1 for t in result if t.lower() == "a-tag") <= 1
        assert sum(1 for t in result if t.lower() == "z-tag") <= 1

    def test_references_order_preserved_and_bounded(self):
        refs = [f"https://ref-{i}.example" for i in range(20)]
        result = _normalize_references(refs)
        assert len(result) <= MAX_REFERENCES
        # Order is preserved (not sorted)
        assert result == refs[:MAX_REFERENCES]

    def test_references_deduped(self):
        refs = ["https://a.com", "https://b.com", "https://A.COM", "https://c.com"]
        result = _normalize_references(refs)
        assert len(result) == 3  # A.COM deduped against a.com

    def test_extracted_results_bounded(self):
        values = [f"value-{i}" for i in range(20)]
        result = _normalize_extracted_results(values)
        assert len(result) <= MAX_EXTRACTED_RESULTS

    def test_empty_list_returns_empty(self):
        assert _normalize_tags([]) == []
        assert _normalize_references(None) == []
        assert _normalize_extracted_results("not a list") == []

    def test_normalize_severity_valid(self):
        assert _normalize_severity("HIGH") == "high"
        assert _normalize_severity("critical") == "critical"
        assert _normalize_severity("Info") == "info"

    def test_normalize_severity_invalid(self):
        assert _normalize_severity("unknown") is None
        assert _normalize_severity("") is None
        assert _normalize_severity(None) is None

    def test_normalize_confidence_valid(self):
        assert _normalize_confidence("HIGH") == "high"
        assert _normalize_confidence("confirmed") == "confirmed"

    def test_normalize_confidence_invalid(self):
        assert _normalize_confidence("unknown") is None
        assert _normalize_confidence("") is None

    def test_normalize_classification_with_hyphenated_keys(self):
        info = {
            "classification": {
                "cve-id": ["CVE-2024-0001"],
                "cwe-id": ["CWE-79"],
            }
        }
        result = _normalize_classification(info)
        assert result == {"cve_ids": ["CVE-2024-0001"], "cwe_ids": ["CWE-79"]}

    def test_normalize_classification_empty(self):
        assert _normalize_classification({"classification": {}}) is None
        assert _normalize_classification({}) is None

    def test_normalize_classification_single_string_values(self):
        """Some nuclei templates emit single string instead of list."""
        info = {
            "classification": {
                "cve-id": "CVE-2024-1234",
                "cwe-id": "CWE-89",
            }
        }
        result = _normalize_classification(info)
        assert result == {"cve_ids": ["CVE-2024-1234"], "cwe_ids": ["CWE-89"]}


# ---------------------------------------------------------------------------
# Tests: Observation builders
# ---------------------------------------------------------------------------

class TestBuildFindingObservation:
    """Verify observation construction from normalized result rows."""

    def test_rich_row_produces_observation(self):
        row = normalize_result_row(RICH_JSONL_ROW)
        obs = build_finding_observation(row)
        assert obs is not None
        assert obs["observation_type"] == "finding.vulnerability_detected"
        assert obs["subject_type"] == "finding.instance"
        assert "finding.instance:" in obs["subject_key"]
        assert obs["payload"]["source"] == "nuclei"
        assert obs["payload"]["detector_id"] == "cve-2024-0001"
        assert obs["payload"]["target_url"] == "https://example.com/login"
        assert obs["payload"]["severity"] == "high"
        assert obs["payload"]["title"] == "Exposed Default Login Page"

    def test_rich_row_observation_includes_classification(self):
        row = normalize_result_row(RICH_JSONL_ROW)
        obs = build_finding_observation(row)
        assert obs["payload"]["classification"] == {
            "cve_ids": ["CVE-2024-0001"],
            "cwe_ids": ["CWE-200"],
        }

    def test_rich_row_observation_includes_tags(self):
        row = normalize_result_row(RICH_JSONL_ROW)
        obs = build_finding_observation(row)
        assert obs["payload"]["tags"] == ["default-login", "exposure", "panel"]

    def test_rich_row_observation_includes_references(self):
        row = normalize_result_row(RICH_JSONL_ROW)
        obs = build_finding_observation(row)
        assert len(obs["payload"]["references"]) == 2

    def test_rich_row_observation_includes_extracted_results(self):
        row = normalize_result_row(RICH_JSONL_ROW)
        obs = build_finding_observation(row)
        assert obs["payload"]["extracted_results"] == ["admin portal", "password field"]

    def test_rich_row_observation_includes_matcher_id(self):
        row = normalize_result_row(RICH_JSONL_ROW)
        obs = build_finding_observation(row)
        assert obs["payload"]["matcher_id"] == "default-login-page"

    def test_minimal_row_produces_observation(self):
        row = normalize_result_row(MINIMAL_JSONL_ROW)
        obs = build_finding_observation(row)
        assert obs is not None
        assert obs["payload"]["source"] == "nuclei"
        assert "classification" not in obs["payload"]
        assert "tags" not in obs["payload"]

    def test_missing_template_id_returns_none(self):
        row = {"target_url": "http://example.com"}
        obs = build_finding_observation(row)
        assert obs is None

    def test_missing_target_url_returns_none(self):
        row = {"template_id": "some-template"}
        obs = build_finding_observation(row)
        assert obs is None

    def test_empty_row_returns_none(self):
        obs = build_finding_observation({})
        assert obs is None

    def test_deterministic_subject_key(self):
        """Same input should produce the same subject_key."""
        row1 = normalize_result_row(RICH_JSONL_ROW)
        row2 = normalize_result_row(RICH_JSONL_ROW)
        obs1 = build_finding_observation(row1)
        obs2 = build_finding_observation(row2)
        assert obs1["subject_key"] == obs2["subject_key"]

    def test_different_matcher_produces_different_key(self):
        """Different matchers on the same template/target should produce different keys."""
        row_a = dict(RICH_JSONL_ROW)
        row_a["matcher-name"] = "matcher-a"
        row_b = dict(RICH_JSONL_ROW)
        row_b["matcher-name"] = "matcher-b"
        obs_a = build_finding_observation(normalize_result_row(row_a))
        obs_b = build_finding_observation(normalize_result_row(row_b))
        assert obs_a["subject_key"] != obs_b["subject_key"]


# ---------------------------------------------------------------------------
# Tests: Transport markers
# ---------------------------------------------------------------------------

class TestTransportMarkers:
    """Verify semantic transport marker constants exist and are correct."""

    def test_schema_version(self):
        assert NUCLEI_SEMANTIC_SCHEMA_VERSION == "nuclei.v1"

    def test_capability_family(self):
        assert NUCLEI_CAPABILITY_FAMILY == "vulnerability_scanning"


# ---------------------------------------------------------------------------
# Tests: End-to-end parse_output integration
# ---------------------------------------------------------------------------

class TestNucleiToolParseOutput:
    """Verify NucleiTool.parse_output() produces enriched metadata end-to-end."""

    def _parse(self, *rows: dict) -> dict:
        from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
            NucleiArgs,
            NucleiTool,
        )
        tool = NucleiTool()
        stdout = _make_jsonl(*rows)
        args = NucleiArgs(target="http://example.com")
        return tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)

    def test_transport_markers_present(self):
        metadata = self._parse(RICH_JSONL_ROW)
        assert metadata["semantic_schema_version"] == NUCLEI_SEMANTIC_SCHEMA_VERSION
        assert metadata["capability_family"] == NUCLEI_CAPABILITY_FAMILY

    def test_results_are_normalized(self):
        metadata = self._parse(RICH_JSONL_ROW)
        row = metadata["results"][0]
        # Normalized keys (underscored, not hyphenated)
        assert "target_url" in row
        assert "template_id" in row
        assert "title" in row
        assert "classification" in row
        assert "tags" in row

    def test_summary_preserved(self):
        metadata = self._parse(RICH_JSONL_ROW, MINIMAL_JSONL_ROW)
        assert metadata["summary"]["total_results"] == 2

    def test_minimal_row_normalized(self):
        metadata = self._parse(MINIMAL_JSONL_ROW)
        row = metadata["results"][0]
        assert row["template_id"] == "tech-detect"
        assert row["severity"] == "info"
        # No rich fields
        assert "classification" not in row
        assert "tags" not in row

    def test_empty_output_has_transport_markers(self):
        from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
            NucleiArgs,
            NucleiTool,
        )
        tool = NucleiTool()
        metadata = tool.parse_output(
            stdout="", stderr="", exit_code=1,
            args=NucleiArgs(target="http://example.com"),
        )
        assert metadata["semantic_schema_version"] == NUCLEI_SEMANTIC_SCHEMA_VERSION
        assert metadata["capability_family"] == NUCLEI_CAPABILITY_FAMILY


# ---------------------------------------------------------------------------
# Tests: Semantic observation emission via NucleiTool
# ---------------------------------------------------------------------------

class TestNucleiToolSemanticEmission:
    """Verify NucleiTool.emit_semantic_observations() produces complete observations."""

    def _emit(self, *rows: dict) -> list[dict]:
        from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
            NucleiArgs,
            NucleiTool,
        )
        tool = NucleiTool()
        stdout = _make_jsonl(*rows)
        args = NucleiArgs(target="http://example.com")
        metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
        return tool.emit_semantic_observations(
            stdout=stdout, stderr="", exit_code=0, args=args, metadata=metadata,
        )

    def test_nuclei_emits_vocab_conformant_evidence(self):
        from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
            NucleiArgs,
            NucleiTool,
        )

        tool = NucleiTool()
        args = NucleiArgs(
            target="https://example.com",
            templates="http/exposures,misconfiguration",
            exclude_templates="cves/2021",
            severity="high,critical",
            threads=40,
            timeout=10,
            rate_limit=100,
        )
        metadata = tool.parse_output(
            stdout=_make_jsonl(RICH_JSONL_ROW, RICH_JSONL_ROW_WITH_CLASSIFICATION),
            stderr="",
            exit_code=0,
            args=args,
        )

        evidence = tool.emit_semantic_evidence(
            stdout="",
            stderr="",
            exit_code=0,
            args=args,
            metadata=metadata,
        )
        valid_entries, dropped_entries = validate_semantic_evidence_entries(evidence)

        assert len(valid_entries) > 0
        assert dropped_entries == []

    def test_nuclei_emit_semantic_evidence_matches_builder_output(self):
        from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
            NucleiArgs,
            NucleiTool,
        )

        tool = NucleiTool()
        args = NucleiArgs(
            target="https://example.com",
            templates="http/exposures",
            severity="high",
            threads=10,
            timeout=5,
        )
        metadata = tool.parse_output(
            stdout=_make_jsonl(RICH_JSONL_ROW),
            stderr="",
            exit_code=0,
            args=args,
        )

        emitted = tool.emit_semantic_evidence(
            stdout="",
            stderr="",
            exit_code=0,
            args=args,
            metadata=metadata,
        )
        expected = build_nuclei_semantic_evidence(metadata, args)

        assert emitted == expected

    def test_rich_row_emits_finding_observation(self):
        observations = self._emit(RICH_JSONL_ROW)
        assert len(observations) == 1
        obs = observations[0]
        assert obs["observation_type"] == "finding.vulnerability_detected"
        assert obs["subject_type"] == "finding.instance"
        assert obs["payload"]["source"] == "nuclei"
        assert obs["payload"]["detector_id"] == "cve-2024-0001"
        assert obs["payload"]["title"] == "Exposed Default Login Page"

    def test_rich_row_observation_has_rich_fields(self):
        observations = self._emit(RICH_JSONL_ROW)
        payload = observations[0]["payload"]
        assert "classification" in payload
        assert "tags" in payload
        assert "references" in payload
        assert "extracted_results" in payload

    def test_multiple_rows_emit_multiple_observations(self):
        observations = self._emit(RICH_JSONL_ROW, RICH_JSONL_ROW_WITH_CLASSIFICATION)
        assert len(observations) == 2

    def test_minimal_row_emits_observation(self):
        observations = self._emit(MINIMAL_JSONL_ROW)
        assert len(observations) == 1
        payload = observations[0]["payload"]
        assert payload["source"] == "nuclei"
        assert "classification" not in payload

    def test_empty_metadata_returns_empty(self):
        from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
            NucleiArgs,
            NucleiTool,
        )
        tool = NucleiTool()
        args = NucleiArgs(target="http://example.com")
        observations = tool.emit_semantic_observations(
            stdout="", stderr="", exit_code=1, args=args, metadata={},
        )
        assert observations == []

    def test_observation_subject_keys_are_deterministic(self):
        obs1 = self._emit(RICH_JSONL_ROW)
        obs2 = self._emit(RICH_JSONL_ROW)
        assert obs1[0]["subject_key"] == obs2[0]["subject_key"]


class TestNucleiSemanticEvidenceBuilder:
    """Verify Task 5.1 builder emits bounded vocabulary-conformant evidence."""

    def _args(self) -> SimpleNamespace:
        return SimpleNamespace(
            target="https://example.com",
            templates="http/exposures,misconfiguration",
            exclude_templates="cves/2021",
            severity="high,critical",
            threads=40,
            timeout=10,
            rate_limit=100,
        )

    def test_build_nuclei_semantic_evidence_does_not_mutate_inputs(self):
        metadata = {
            "results": [normalize_result_row(RICH_JSONL_ROW)],
            "summary": {"templates": 120},
        }
        args = self._args()
        metadata_before = copy.deepcopy(metadata)
        args_before = copy.deepcopy(vars(args))

        _ = build_nuclei_semantic_evidence(metadata, args)

        assert metadata == metadata_before
        assert vars(args) == args_before

    def test_build_nuclei_semantic_evidence_includes_expected_types(self):
        metadata = {
            "results": [
                normalize_result_row(RICH_JSONL_ROW),
                normalize_result_row(RICH_JSONL_ROW_WITH_CLASSIFICATION),
            ],
            "summary": {"templates": 120},
        }
        evidence = build_nuclei_semantic_evidence(metadata, self._args())
        types = {entry["type"] for entry in evidence}

        assert "variant" in types
        assert "execution_parameter" in types
        assert "matcher_or_filter" in types
        assert "result_summary" in types
        assert "target_template" in types

        valid_entries, dropped_entries = validate_semantic_evidence_entries(evidence)
        assert len(valid_entries) > 0
        assert dropped_entries == []

    def test_build_nuclei_semantic_evidence_includes_bounded_severity_counts(self):
        metadata = {
            "results": [
                {"severity": "critical"},
                {"severity": "high"},
                {"severity": "high"},
                {"severity": "low"},
            ],
            "summary": {"templates": 120},
        }

        evidence = build_nuclei_semantic_evidence(metadata, self._args())
        severity_entry = next(
            item
            for item in evidence
            if item.get("type") == "result_summary"
            and item.get("name") == "findings_by_severity"
        )
        assert severity_entry["value"] == "critical=1,high=2,low=1"

    def test_build_nuclei_semantic_evidence_adds_zero_findings_diagnostic(self):
        metadata = {"results": [], "summary": {"templates": 42}}
        evidence = build_nuclei_semantic_evidence(metadata, self._args())
        diagnostic = next(
            item
            for item in evidence
            if item.get("type") == "diagnostic"
            and item.get("name") == "no_findings_with_templates"
        )
        assert diagnostic["value"] == 42


class _PromptEchoLLM:
    """Deterministic fake LLM that exposes prompt bytes through structured output."""

    model = "test-model"

    def __init__(self) -> None:
        self.last_prompt: str | None = None

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs):
        self.last_prompt = user_prompt
        digest = hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()
        return SimpleNamespace(
            content="",
            structured_output={
                "summary": f"prompt-sha256:{digest}",
                "key_findings": [f"prompt-len:{len(user_prompt)}"],
                "structured_signals": [],
                "decision_evidence": [digest[:16]],
                "lossiness_risk": "low",
            },
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


def test_nuclei_prompt_baseline_frozen_v4_with_evidence():
    from agent.context.tool_processor import UniversalToolProcessor
    from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
        NucleiArgs,
        NucleiTool,
    )

    llm = _PromptEchoLLM()
    processor = UniversalToolProcessor(
        llm_client=llm,
        logger=logging.getLogger("test.nuclei.prompt.baseline.pre_v4"),
    )
    tool = NucleiTool()
    stdout = _make_jsonl(RICH_JSONL_ROW)
    args = NucleiArgs(target="http://example.com")

    parsed_metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    semantic_observations = tool.emit_semantic_observations(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )
    semantic_evidence = tool.emit_semantic_evidence(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )
    metadata = {
        "tool_metadata": {
            **parsed_metadata,
            "semantic_observations": semantic_observations,
            "semantic_evidence": semantic_evidence,
        }
    }

    result = asyncio.run(
        processor.process_output(
            "web_applications.web_vulnerability_scanners.nuclei",
            stdout,
            metadata=metadata,
        )
    )

    assert llm.last_prompt is not None
    # Rebaselined 2026-04-21 after centralizing evidence normalization through
    # validate_semantic_evidence_entries so extract_runtime_semantic_inputs no
    # longer bypasses the validator. The canonical detail={} field now reaches
    # the prompt for all emitter-produced evidence.
    assert len(llm.last_prompt) == 10966
    assert (
        hashlib.sha256(llm.last_prompt.encode("utf-8")).hexdigest()
        == "adbcc239ed00bf3dea32dec1a541289b63e53a3047e4081ce388791dce814b98"
    )
    assert "finding.vulnerability_detected" in llm.last_prompt
    assert '"result_summary":[' in llm.last_prompt

    assert result.summary == "prompt-sha256:adbcc239ed00bf3dea32dec1a541289b63e53a3047e4081ce388791dce814b98"
    assert result.key_findings == ["prompt-len:10966"]
    assert result.structured_signals == []
    assert result.decision_evidence == ["adbcc239ed00bf3d"]
    assert result.lossiness_risk == "low"


def test_nuclei_prompt_includes_severity_counts():
    from agent.context.tool_processor import UniversalToolProcessor
    from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
        NucleiArgs,
        NucleiTool,
    )

    llm = _PromptEchoLLM()
    processor = UniversalToolProcessor(
        llm_client=llm,
        logger=logging.getLogger("test.nuclei.prompt.severity_counts"),
    )
    tool = NucleiTool()
    stdout = _make_jsonl(RICH_JSONL_ROW, RICH_JSONL_ROW_WITH_CLASSIFICATION)
    args = NucleiArgs(target="http://example.com")
    parsed_metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    semantic_observations = tool.emit_semantic_observations(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )
    semantic_evidence = tool.emit_semantic_evidence(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=parsed_metadata,
    )

    asyncio.run(
        processor.process_output(
            "web_applications.web_vulnerability_scanners.nuclei",
            stdout,
            metadata={
                "tool_metadata": {
                    **parsed_metadata,
                    "semantic_observations": semantic_observations,
                    "semantic_evidence": semantic_evidence,
                }
            },
        )
    )

    assert llm.last_prompt is not None
    assert '"result_summary":[' in llm.last_prompt
    assert '"name":"findings_by_severity"' in llm.last_prompt


def test_nuclei_no_observation_regressions():
    from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
        NucleiArgs,
        NucleiTool,
    )

    tool = NucleiTool()
    stdout = _make_jsonl(RICH_JSONL_ROW, RICH_JSONL_ROW_WITH_CLASSIFICATION)
    args = NucleiArgs(target="http://example.com")
    metadata = tool.parse_output(stdout=stdout, stderr="", exit_code=0, args=args)
    observations = tool.emit_semantic_observations(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )
    digest = hashlib.sha256(
        json.dumps(observations, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert digest == "ddeb831adf38ee3d04d76d094b63121ff7ce5b52bfa0d95ab30c258cd34e2fe5"
