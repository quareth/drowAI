"""Tests for deterministic ffuf backend knowledge adapter behavior."""

from __future__ import annotations

from backend.services.knowledge.adapters.base import AdapterContext
from backend.services.knowledge.adapters.ffuf_adapter import FfufKnowledgeAdapter


def _build_context(
    *,
    tool_name: str,
    tool_metadata: dict | None = None,
    semantic_observations: list[dict] | None = None,
    semantic_evidence: list[dict] | None = None,
    artifacts: list[dict] | None = None,
) -> AdapterContext:
    evidence_archives = [
        {
            "id": f"archive-{artifact['artifact_id']}",
            "source_artifact_id": artifact["artifact_id"],
            "lineage": {"artifact_id": artifact["artifact_id"]},
        }
        for artifact in artifacts or []
        if isinstance(artifact.get("artifact_id"), str)
    ]
    execution_metadata: dict = {"tool_metadata": tool_metadata or {}}
    if semantic_observations is not None:
        execution_metadata["semantic_observations"] = semantic_observations
    if semantic_evidence is not None:
        execution_metadata["semantic_evidence"] = semantic_evidence
    execution_payload = {
        "execution": {
            "execution_id": "exec-ffuf-1",
            "tool_name": tool_name,
            "tool_arguments": {"target": "https://example.com/FUZZ"},
            "execution_metadata": execution_metadata,
        },
        "artifacts": artifacts or [],
    }
    return AdapterContext(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="exec-ffuf-1",
        ingestion_run_id="run-ffuf-1",
        execution_payload=execution_payload,
        tool_metadata=tool_metadata or {},
        semantic_observations=semantic_observations or [],
        semantic_evidence=semantic_evidence or [],
        artifact_summaries=artifacts or [],
        evidence_archives=evidence_archives,
    )


def test_ffuf_crawler_prefers_semantic_observations() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_crawlers.ffuf",
        semantic_observations=[
            {
                "observation_type": "web.path_discovered",
                "subject_type": "web.path",
                "subject_key": "web.path:https://example.com/ignored",
                "payload": {"url": "https://example.com/semantic-path", "status_code": 200},
            }
        ],
        tool_metadata={"results": [{"url": "https://example.com/metadata-path", "status": 200}]},
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    assert observations[0].subject_key == "web.path:https://example.com/semantic-path"
    assert observations[0].payload.get("path") == "/semantic-path"


def test_ffuf_crawler_normalizes_semantic_rows_through_backend_helper() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_crawlers.ffuf",
        semantic_observations=[
            {
                "observation_type": "web.path_discovered",
                "subject_type": "web.path",
                "subject_key": "web.path:https://do-not-trust-this-subject-key",
                "payload": {"url": "HTTP://Example.com:80//Admin?debug=1#frag"},
            }
        ],
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    assert observations[0].subject_key == "web.path:http://example.com/Admin"


def test_ffuf_crawler_bypasses_extract_semantic_observations_for_path_rows() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_crawlers.ffuf",
        semantic_observations=[
            {
                "observation_type": "web.path_discovered",
                "subject_type": "web.path",
                "subject_key": "web.path:http://example.com/untrusted",
                "payload": {"url": "https://example.com/rebuilt"},
            }
        ],
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    assert observations[0].subject_key == "web.path:https://example.com/rebuilt"
    assert observations[0].subject_key != "web.path:http://example.com/untrusted"


def test_ffuf_fuzzer_reads_results_from_tool_metadata() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_application_fuzzers.ffuf",
        tool_metadata={
            "results": [
                {
                    "url": "https://example.com/data/42",
                    "status": 200,
                    "length": 512,
                }
            ]
        },
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    assert observations[0].subject_key == "web.path:https://example.com/data/42"
    assert observations[0].payload.get("status_code") == 200
    assert observations[0].payload.get("response_size") == 512


def test_ffuf_fuzzer_reads_results_from_json_artifact_when_metadata_missing() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_application_fuzzers.ffuf",
        tool_metadata={},
        artifacts=[
            {
                "artifact_id": "artifact-ffuf-json",
                "artifact_kind": "stdout",
                "content_text": (
                    '{"results":[{"url":"https://example.com/data/7","status":200,"length":77}]}\n'
                    "non-json trailer"
                ),
            }
        ],
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    assert observations[0].subject_key == "web.path:https://example.com/data/7"
    assert observations[0].payload.get("response_size") == 77


def test_ffuf_adapter_marks_calibrated_rows_from_semantic_evidence() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_application_fuzzers.ffuf",
        tool_metadata={
            "results": [
                {
                    "url": "https://example.com/admin",
                    "status": 200,
                    "length": 123,
                }
            ]
        },
        semantic_evidence=[
            {"type": "baseline", "name": "autocalibration", "value": True},
            {
                "type": "matcher_or_filter",
                "name": "calibrated_filter_group",
                "value": "status=200,size=123",
            },
        ],
    )

    observations = adapter.extract(context)
    assert len(observations) == 1
    assert observations[0].payload.get("calibrated") is True


def test_ffuf_adapter_never_emits_path_from_evidence_only() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_application_fuzzers.ffuf",
        semantic_evidence=[
            {"type": "baseline", "name": "autocalibration", "value": True},
            {
                "type": "matcher_or_filter",
                "name": "calibrated_filter_group",
                "value": "status=200,size=123",
            },
        ],
    )

    assert adapter.extract(context) == []


def test_ffuf_adapter_respects_per_origin_cap() -> None:
    adapter = FfufKnowledgeAdapter()
    results = [{"url": "https://example.com/important", "status": 200, "length": 1}]
    results.extend(
        {"url": f"https://example.com/p{index:03d}", "status": 500, "length": index}
        for index in range(205)
    )
    context = _build_context(
        tool_name="web_applications.web_application_fuzzers.ffuf",
        tool_metadata={"results": results},
    )

    observations = adapter.extract(context)
    assert len(observations) == 200
    subject_keys = {item.subject_key for item in observations}
    assert "web.path:https://example.com/important" in subject_keys
    assert "web.path:https://example.com/p204" not in subject_keys


def test_ffuf_adapter_hard_drops_likely_soft_404_rows_before_observation_creation() -> None:
    adapter = FfufKnowledgeAdapter()
    context = _build_context(
        tool_name="web_applications.web_application_fuzzers.ffuf",
        tool_metadata={
            "results": [
                {"url": "https://example.com/soft-404", "status": 404, "length": 120},
                {"url": "https://example.com/keep-404", "status": 404, "length": 512},
                {"url": "https://example.com/ok", "status": 200, "length": 256},
            ]
        },
    )

    observations = adapter.extract(context)
    subject_keys = {item.subject_key for item in observations}

    assert "web.path:https://example.com/soft-404" not in subject_keys
    assert "web.path:https://example.com/keep-404" in subject_keys
    assert "web.path:https://example.com/ok" in subject_keys
