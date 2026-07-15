"""Tests for report-level source watermark aggregation."""

from __future__ import annotations

import copy
import json

import pytest

from backend.services.reporting.contracts import (
    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY,
    GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY,
)
from backend.services.reporting.source_watermark_service import (
    ReportSourceMemoWatermarkInput,
    build_report_source_generation_metadata,
    build_report_source_watermark,
)


def _selected_memos() -> tuple[ReportSourceMemoWatermarkInput, ...]:
    return (
        ReportSourceMemoWatermarkInput(
            memo_id="memo-b",
            version=2,
            source_watermark={
                "schema_version": 1,
                "sources": {"chat_messages": {"latest_id": 12}},
            },
        ),
        ReportSourceMemoWatermarkInput(
            memo_id="memo-a",
            version=1,
            source_watermark={
                "sources": {"tool_executions": {"latest_id": "tool-1"}},
                "schema_version": 1,
            },
        ),
    )


def test_report_source_watermark_is_stable_and_preserves_selected_memo_detail() -> None:
    first = build_report_source_watermark(
        report_type="pentest",
        selected_memos=_selected_memos(),
        include_candidate_findings=False,
    )
    second = build_report_source_watermark(
        report_type="pentest",
        selected_memos=tuple(reversed(_selected_memos())),
        include_candidate_findings=False,
    )

    assert first == second
    assert first["selected_memos"] == [
        {
            "memo_id": "memo-a",
            "version": 1,
            "source_watermark": {
                "schema_version": 1,
                "sources": {"tool_executions": {"latest_id": "tool-1"}},
            },
        },
        {
            "memo_id": "memo-b",
            "version": 2,
            "source_watermark": {
                "schema_version": 1,
                "sources": {"chat_messages": {"latest_id": 12}},
            },
        },
    ]
    assert first["hash_algorithm"] == "sha256"
    assert isinstance(first["hash"], str)
    assert len(first["hash"]) == 64
    assert json.loads(json.dumps(first, sort_keys=True)) == first


def test_report_source_watermark_hash_changes_for_each_source_input() -> None:
    baseline = build_report_source_watermark(
        report_type="pentest",
        selected_memos=_selected_memos(),
        include_candidate_findings=False,
    )["hash"]

    cases: list[tuple[str, tuple[ReportSourceMemoWatermarkInput, ...], bool]] = []
    changed_id = list(_selected_memos())
    changed_id[0] = ReportSourceMemoWatermarkInput(
        memo_id="memo-c",
        version=changed_id[0].version,
        source_watermark=changed_id[0].source_watermark,
    )
    cases.append(("pentest", tuple(changed_id), False))

    changed_version = list(_selected_memos())
    changed_version[0] = ReportSourceMemoWatermarkInput(
        memo_id=changed_version[0].memo_id,
        version=3,
        source_watermark=changed_version[0].source_watermark,
    )
    cases.append(("pentest", tuple(changed_version), False))

    changed_source = list(_selected_memos())
    source_watermark = copy.deepcopy(dict(changed_source[0].source_watermark))
    source_watermark["sources"]["chat_messages"]["latest_id"] = 13
    changed_source[0] = ReportSourceMemoWatermarkInput(
        memo_id=changed_source[0].memo_id,
        version=changed_source[0].version,
        source_watermark=source_watermark,
    )
    cases.append(("pentest", tuple(changed_source), False))
    cases.append(("vulnerability_assessment", _selected_memos(), False))
    cases.append(("pentest", _selected_memos(), True))

    for report_type, selected_memos, include_candidate_findings in cases:
        watermark = build_report_source_watermark(
            report_type=report_type,
            selected_memos=selected_memos,
            include_candidate_findings=include_candidate_findings,
        )
        assert watermark["hash"] != baseline


def test_report_source_watermark_rejects_duplicate_memo_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        build_report_source_watermark(
            report_type="pentest",
            selected_memos=(
                ReportSourceMemoWatermarkInput(
                    memo_id="memo-a",
                    version=1,
                    source_watermark={"schema_version": 1},
                ),
                ReportSourceMemoWatermarkInput(
                    memo_id="memo-a",
                    version=2,
                    source_watermark={"schema_version": 1},
                ),
            ),
            include_candidate_findings=False,
        )


def test_report_source_generation_metadata_represents_persisted_report_keys() -> None:
    watermark = build_report_source_watermark(
        report_type="pentest",
        selected_memos=_selected_memos(),
        include_candidate_findings=False,
    )

    metadata = build_report_source_generation_metadata(watermark)

    assert metadata == {
        GENERATION_METADATA_SOURCE_WATERMARK_SCHEMA_VERSION_KEY: watermark[
            "schema_version"
        ],
        GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: watermark["hash"],
    }
