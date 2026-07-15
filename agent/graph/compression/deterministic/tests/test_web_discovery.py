"""Unit tests for web-discovery deterministic compression helpers."""

from __future__ import annotations

import json

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)
from agent.graph.compression.deterministic.web_discovery import (
    FFUF_CRAWLER_TOOL_ID,
    registered_web_discovery_tool_ids,
    web_discovery_adapter,
)
from agent.tools.web_applications._ffuf_common import parse_ffuf_json_text


def test_web_discovery_adapter_registers_ffuf_crawler() -> None:
    """Visible ffuf crawler resolves to the deterministic web-discovery adapter."""

    assert registered_web_discovery_tool_ids() == (FFUF_CRAWLER_TOOL_ID,)
    assert get_adapter(FFUF_CRAWLER_TOOL_ID) is web_discovery_adapter


def test_ffuf_json_metadata_summary_ranked_findings_and_evidence() -> None:
    """Parsed ffuf JSON produces grouped endpoint findings and evidence."""

    metadata = parse_ffuf_json_text(
        json.dumps(
            {
                "config": {"url": "https://example.com/FUZZ"},
                "results": [
                    {
                        "url": "https://example.com/admin",
                        "status": 200,
                        "length": 123,
                        "words": 10,
                        "lines": 2,
                    },
                    {
                        "url": "https://example.com/private",
                        "status": 403,
                        "length": 999,
                        "words": 20,
                        "lines": 4,
                    },
                    {
                        "url": "https://example.com/login",
                        "status": 302,
                        "length": 51,
                        "words": 8,
                        "lines": 1,
                    },
                    {
                        "url": "https://example.com/debug",
                        "status": 500,
                        "length": 1001,
                        "words": 30,
                        "lines": 5,
                    },
                    {
                        "url": "https://example.com/assets",
                        "status": 200,
                        "length": 50,
                        "words": 6,
                        "lines": 1,
                    },
                    {
                        "url": "https://example.com/missing",
                        "status": 404,
                        "length": 10,
                        "words": 2,
                        "lines": 1,
                    },
                ],
            }
        )
    )
    metadata.update(
        {
            "status_distribution": {"200": 2, "302": 1, "403": 1, "404": 1, "500": 1},
            "results_truncated": True,
            "total_results": 12,
        }
    )

    result = compress_deterministically(
        CompressionInput(
            tool_name=FFUF_CRAWLER_TOOL_ID,
            raw_result={
                "metadata": metadata,
                "parameters": {"target": "https://example.com/FUZZ"},
                "artifacts": [
                    "artifacts/ffuf_crawler.json",
                    {
                        "path": "https://objects.local/private/ffuf.json?X-Amz-Signature=raw",
                        "artifact_id": "artifact-1",
                        "artifact_kind": "object_store",
                    },
                ],
            },
        )
    )

    assert result.summary == (
        "ffuf crawler discovered 6 endpoints for https://example.com/FUZZ; "
        "grouped into 6 response fingerprints."
    )
    assert result.key_findings[:5] == (
        "group count=1 status=200 size=123 words=10 lines=2 examples=/admin",
        "group count=1 status=200 size=50 words=6 lines=1 examples=/assets",
        "group count=1 status=403 size=999 words=20 lines=4 examples=/private",
        "group count=1 status=500 size=1001 words=30 lines=5 examples=/debug",
        "group count=1 status=302 size=51 words=8 lines=1 examples=/login",
    )
    assert "ffuf top results truncated: showing 5 of 6." not in result.key_findings
    assert (
        "grouped 6 results into 6 response fingerprints; showing 6 groups."
        in result.key_findings
    )
    assert "status distribution: 200=2, 302=1, 403=1, 404=1, 500=1" in result.key_findings
    assert "results_truncated: true" in result.key_findings
    assert "total_results: 12" in result.key_findings
    assert "artifact: artifacts/ffuf_crawler.json" in result.key_findings
    assert "artifact: artifact://artifact-1" in result.key_findings
    assert result.decision_evidence[:5] == (
        "ffuf group: count=1 status=200 size=123 words=10 lines=2 examples=/admin",
        "ffuf group: count=1 status=200 size=50 words=6 lines=1 examples=/assets",
        "ffuf group: count=1 status=403 size=999 words=20 lines=4 examples=/private",
        "ffuf group: count=1 status=500 size=1001 words=30 lines=5 examples=/debug",
        "ffuf group: count=1 status=302 size=51 words=8 lines=1 examples=/login",
    )
    assert result.structured_signals == (
        {"type": "kv_pair", "key": "ffuf_endpoint_count", "value": 6},
        {"type": "kv_pair", "key": "ffuf_group_count", "value": 6},
        {"type": "kv_pair", "key": "ffuf_target", "value": "https://example.com/FUZZ"},
        {
            "type": "kv_pair",
            "key": "ffuf_status_distribution",
            "value": "200=2,302=1,403=1,404=1,500=1",
        },
        {"type": "kv_pair", "key": "ffuf_results_truncated", "value": True},
        {"type": "kv_pair", "key": "ffuf_total_results", "value": 12},
        {
            "type": "kv_pair",
            "key": "ffuf_artifact_ref",
            "value": "artifacts/ffuf_crawler.json",
        },
        {
            "type": "kv_pair",
            "key": "ffuf_artifact_ref",
            "value": "artifact://artifact-1",
        },
    )
    assert result.completeness == "partial"
    assert result.lossiness_risk == "low"


def test_ffuf_text_stdout_is_parsed_when_metadata_is_absent() -> None:
    """ffuf terminal rows are parsed into endpoint facts when metadata is absent."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=FFUF_CRAWLER_TOOL_ID,
            raw_result={
                "stdout": "https://example.com/admin 200 321 22 7",
                "parameters": {"target": "https://example.com/FUZZ"},
            },
        )
    )

    assert result.summary == (
        "ffuf crawler discovered 1 endpoints for https://example.com/FUZZ; "
        "grouped into 1 response fingerprints."
    )
    assert result.key_findings[0] == (
        "group count=1 status=200 size=321 words=22 lines=7 examples=/admin"
    )
    assert result.decision_evidence[0] == (
        "ffuf group: count=1 status=200 size=321 words=22 lines=7 examples=/admin"
    )


def test_ffuf_empty_results_produce_explicit_bounded_summary() -> None:
    """A successful empty ffuf run does not fall through to no-result."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=FFUF_CRAWLER_TOOL_ID,
            raw_result={
                "metadata": {
                    "ffuf_variant": "crawler",
                    "config": {"url": "https://example.com/FUZZ"},
                    "results": [],
                },
            },
        )
    )

    assert result.summary == "ffuf crawler discovered 0 endpoints for https://example.com/FUZZ."
    assert result.key_findings == ("ffuf crawler returned no discovered endpoints.",)
    assert result.structured_signals == (
        {"type": "kv_pair", "key": "ffuf_endpoint_count", "value": 0},
        {"type": "kv_pair", "key": "ffuf_group_count", "value": 0},
        {"type": "kv_pair", "key": "ffuf_target", "value": "https://example.com/FUZZ"},
    )


def test_ffuf_parse_error_and_timeout_are_bounded_errors() -> None:
    """ffuf parse errors and execution timeouts produce compact error context."""

    parse_error = compress_deterministically(
        CompressionInput(
            tool_name=FFUF_CRAWLER_TOOL_ID,
            raw_result={
                "metadata": {
                    "ffuf_variant": "crawler",
                    "config": {"url": "https://example.com/FUZZ"},
                    "error": "Failed to parse ffuf JSON: bad payload",
                    "results": [],
                },
            },
        )
    )
    timeout = compress_deterministically(
        CompressionInput(
            tool_name=FFUF_CRAWLER_TOOL_ID,
            raw_result={
                "success": False,
                "metadata": {
                    "ffuf_variant": "crawler",
                    "config": {"url": "https://example.com/FUZZ"},
                    "results": [],
                    "timeout": {
                        "message": "ffuf timed out; reduce the wordlist size, lower rate, or raise job_max_time."
                    },
                },
            },
        )
    )

    assert parse_error.summary == (
        "ffuf crawler discovered 0 endpoints for https://example.com/FUZZ; "
        "error: Failed to parse ffuf JSON: bad payload."
    )
    assert parse_error.errors == ("Failed to parse ffuf JSON: bad payload",)
    assert parse_error.structured_signals == (
        {
            "type": "error_context",
            "message": "ffuf crawler failed: Failed to parse ffuf JSON: bad payload",
        },
    )
    assert timeout.summary == (
        "ffuf crawler discovered 0 endpoints for https://example.com/FUZZ; "
        "error: ffuf timed out; reduce the wordlist size, lower rate, or raise job_max_time."
    )
    assert timeout.errors == (
        "ffuf timed out; reduce the wordlist size, lower rate, or raise job_max_time.",
    )


def test_ffuf_numeric_id_fuzzing_collapses_identical_responses_to_ranges() -> None:
    """Numeric FUZZ inputs are compacted into response groups and ranges."""

    results = [
        _ffuf_row("http://10.129.35.155:80/data/1", "1", 200, 17144, 7066, 371),
        _ffuf_row("http://10.129.35.155:80/data/2", "2", 200, 17144, 7066, 371),
    ]
    results.extend(
        _ffuf_row(
            f"http://10.129.35.155:80/data/{number}",
            str(number),
            302,
            208,
            21,
            4,
            redirect="http://10.129.35.155/",
        )
        for number in range(3, 51)
    )

    result = _compress_ffuf(
        results,
        target="http://10.129.35.155:80/data/FUZZ",
        artifacts=["artifacts/ffuf_data_1_50.json"],
    )

    assert result.summary == (
        "ffuf crawler discovered 50 endpoints for http://10.129.35.155:80/data/FUZZ; "
        "grouped into 2 response fingerprints."
    )
    assert result.key_findings[:2] == (
        "group count=2 status=200 size=17144 words=7066 lines=371 inputs=1-2 "
        "examples=/data/1,/data/2",
        "group count=48 status=302 size=208 words=21 lines=4 "
        "redirect=http://10.129.35.155/ inputs=3-50 "
        "examples=/data/3,/data/4,/data/5,/data/6,/data/50",
    )
    assert (
        "grouped 50 results into 2 response fingerprints; showing 2 groups."
        in result.key_findings
    )
    assert "status distribution: 200=2, 302=48" in result.key_findings
    assert "artifact: artifacts/ffuf_data_1_50.json" in result.key_findings
    assert {"type": "kv_pair", "key": "ffuf_group_count", "value": 2} in result.structured_signals
    assert {
        "type": "kv_pair",
        "key": "ffuf_artifact_ref",
        "value": "artifacts/ffuf_data_1_50.json",
    } in result.structured_signals


def test_ffuf_path_discovery_groups_repeated_baseline_redirects() -> None:
    """Path discovery keeps rare endpoints while folding baseline redirects."""

    results = [
        _ffuf_row("https://example.com/admin", "admin", 200, 850, 44, 12),
        _ffuf_row("https://example.com/login", "login", 200, 620, 35, 9),
        _ffuf_row("https://example.com/uploads", "uploads", 403, 180, 12, 3),
    ]
    results.extend(
        _ffuf_row(
            f"https://example.com/{name}",
            name,
            302,
            154,
            8,
            2,
            redirect="https://example.com/",
        )
        for name in ("assets-old", "backup-old", "dev-old", "tmp-old")
    )

    result = _compress_ffuf(results)

    assert result.key_findings[:4] == (
        "group count=1 status=200 size=850 words=44 lines=12 inputs=admin examples=/admin",
        "group count=1 status=200 size=620 words=35 lines=9 inputs=login examples=/login",
        "group count=1 status=403 size=180 words=12 lines=3 inputs=uploads examples=/uploads",
        "group count=4 status=302 size=154 words=8 lines=2 redirect=https://example.com/ "
        "inputs=assets-old,backup-old,dev-old,tmp-old "
        "examples=/assets-old,/backup-old,/dev-old,/tmp-old",
    )


def test_ffuf_auth_discovery_groups_auth_barriers_and_login_redirects() -> None:
    """401, 403, and login redirects remain distinct response fingerprints."""

    result = _compress_ffuf(
        [
            _ffuf_row("https://example.com/api/admin", "api/admin", 401, 92, 5, 1),
            _ffuf_row("https://example.com/admin", "admin", 403, 120, 7, 2),
            _ffuf_row(
                "https://example.com/private",
                "private",
                302,
                160,
                9,
                2,
                redirect="https://example.com/login",
            ),
        ]
    )

    assert result.key_findings[:3] == (
        "group count=1 status=403 size=120 words=7 lines=2 inputs=admin examples=/admin",
        "group count=1 status=401 size=92 words=5 lines=1 inputs=api/admin examples=/api/admin",
        "group count=1 status=302 size=160 words=9 lines=2 "
        "redirect=https://example.com/login inputs=private examples=/private",
    )


def test_ffuf_soft_404_noise_is_grouped_as_one_large_fingerprint() -> None:
    """Many same-shape 200 responses are represented as one noisy group."""

    result = _compress_ffuf(
        [
            _ffuf_row(f"https://example.com/{number}", str(number), 200, 1234, 90, 20)
            for number in range(1, 26)
        ]
    )

    assert result.key_findings[0] == (
        "group count=25 status=200 size=1234 words=90 lines=20 inputs=1-25 "
        "examples=/1,/2,/3,/4,/25"
    )
    assert (
        "grouped 25 results into 1 response fingerprints; showing 1 groups."
        in result.key_findings
    )


def test_ffuf_backup_config_hits_rank_above_large_repetitive_groups() -> None:
    """Rare 2xx backup/config hits rank ahead of repetitive redirect groups."""

    results = [_ffuf_row("https://example.com/.env", ".env", 200, 230, 18, 5)]
    results.extend(
        _ffuf_row(
            f"https://example.com/miss-{number}",
            f"miss-{number}",
            302,
            180,
            12,
            3,
            redirect="https://example.com/",
        )
        for number in range(30)
    )

    result = _compress_ffuf(results)

    assert result.key_findings[0] == (
        "group count=1 status=200 size=230 words=18 lines=5 inputs=.env examples=/.env"
    )
    assert result.key_findings[1].startswith("group count=30 status=302")


def test_ffuf_non_numeric_inputs_use_examples_without_range_synthesis() -> None:
    """Non-numeric FUZZ inputs are shown as bounded input examples."""

    result = _compress_ffuf(
        [
            _ffuf_row("https://example.com/admin", "admin", 200, 100, 10, 2),
            _ffuf_row("https://example.com/login", "login", 200, 100, 10, 2),
            _ffuf_row(
                "https://example.com/config.php.bak",
                "config.php.bak",
                200,
                100,
                10,
                2,
            ),
        ]
    )

    assert result.key_findings[0] == (
        "group count=3 status=200 size=100 words=10 lines=2 "
        "inputs=admin,login,config.php.bak examples=/admin,/login,/config.php.bak"
    )
    assert "inputs=1-3" not in result.key_findings[0]


def test_ffuf_many_unique_fingerprints_reports_group_limit_and_artifact() -> None:
    """Many unique response fingerprints are limited with an artifact pointer."""

    result = _compress_ffuf(
        [
            _ffuf_row(
                f"https://example.com/item-{number}",
                str(number),
                200,
                1000 + number,
                100 + number,
                10 + number,
            )
            for number in range(20)
        ],
        artifacts=["artifacts/ffuf_many.json"],
    )

    assert (
        "grouped 20 results into 20 response fingerprints; showing 12 of 20 groups; "
        "full results in artifact."
        in result.key_findings
    )
    assert "artifact: artifacts/ffuf_many.json" in result.key_findings
    assert len([line for line in result.key_findings if line.startswith("group ")]) == 12


def test_ffuf_missing_partial_fields_group_with_available_facts() -> None:
    """Partial ffuf rows still group using available status and URL data."""

    result = _compress_ffuf(
        [
            {"url": "https://example.com/partial", "status": 204},
            {"url": "https://example.com/partial-two", "status": 204},
        ]
    )

    assert result.key_findings[0] == (
        "group count=2 status=204 examples=/partial,/partial-two"
    )
    assert "status distribution: 204=2" in result.key_findings


def test_ffuf_grouped_output_does_not_dump_full_raw_result_lists() -> None:
    """Compact findings and evidence expose groups instead of raw row lists."""

    result = _compress_ffuf(
        [
            _ffuf_row(f"https://example.com/{number}", str(number), 302, 10, 2, 1)
            for number in range(40)
        ]
    )

    assert len(result.key_findings) < 10
    assert all("ffuf record:" not in line for line in result.decision_evidence)
    assert result.decision_evidence[0] == (
        "ffuf group: count=40 status=302 size=10 words=2 lines=1 inputs=0-39 "
        "examples=/0,/1,/2,/3,/39"
    )


def _compress_ffuf(
    results: list[dict[str, object]],
    *,
    target: str = "https://example.com/FUZZ",
    artifacts: list[str] | None = None,
):
    return compress_deterministically(
        CompressionInput(
            tool_name=FFUF_CRAWLER_TOOL_ID,
            raw_result={
                "metadata": {
                    "ffuf_variant": "crawler",
                    "config": {"url": target},
                    "results": results,
                },
                "artifacts": artifacts or [],
            },
        )
    )


def _ffuf_row(
    url: str,
    input_value: str,
    status: int,
    length: int,
    words: int,
    lines: int,
    *,
    redirect: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "url": url,
        "input": {"FUZZ": input_value},
        "status": status,
        "length": length,
        "words": words,
        "lines": lines,
    }
    if redirect:
        row["redirectlocation"] = redirect
    return row
