"""Unit tests for CVE source release resolution and ZIP download behavior."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from backend.services.cve_indexing.source_client import CveSourceClient, CveSourceClientError


class _FakeResponse:
    def __init__(self, *, payload: Any | None = None, content: bytes = b"", status_code: int = 200) -> None:
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise CveSourceClientError(f"HTTP status {self.status_code}")

    def json(self) -> Any:
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def _asset(name: str, url: str, updated_at: str = "2026-03-15T10:00:00Z") -> dict[str, str]:
    return {"name": name, "browser_download_url": url, "updated_at": updated_at}


def test_resolve_latest_baseline_asset_picks_newest_day() -> None:
    releases_payload = [
        {
            "assets": [
                _asset("2026-03-14_all_CVEs_at_end_of_day.zip", "https://example.test/2026-03-14-baseline.zip"),
                _asset("2026-03-15_delta_CVEs_at_11.00z.zip", "https://example.test/2026-03-15-11.zip"),
            ]
        },
        {
            "assets": [
                _asset("2026-03-15_all_CVEs_at_end_of_day.zip", "https://example.test/2026-03-15-baseline.zip"),
            ]
        },
    ]
    session = _FakeSession([_FakeResponse(payload=releases_payload)])
    client = CveSourceClient(session=session)

    baseline = client.resolve_latest_baseline_asset()

    assert baseline.baseline_day == date(2026, 3, 15)
    assert baseline.download_url == "https://example.test/2026-03-15-baseline.zip"
    assert baseline.delta_hour_utc is None


def test_resolve_missing_delta_assets_filters_applied_and_sorts() -> None:
    releases_payload = [
        {
            "assets": [
                _asset("2026-03-15_delta_CVEs_at_11.00z.zip", "https://example.test/2026-03-15-11.zip"),
                _asset("2026-03-15_delta_CVEs_at_10.00z.zip", "https://example.test/2026-03-15-10.zip"),
                _asset("2026-03-15_delta_CVEs_at_12.00z.zip", "https://example.test/2026-03-15-12.zip"),
                _asset("2026-03-14_delta_CVEs_at_23.00z.zip", "https://example.test/2026-03-14-23.zip"),
            ]
        }
    ]
    session = _FakeSession([_FakeResponse(payload=releases_payload)])
    client = CveSourceClient(session=session)

    missing = client.resolve_missing_delta_assets(
        baseline_day=date(2026, 3, 15),
        applied_hours=(datetime(2026, 3, 15, 10, tzinfo=UTC),),
    )

    assert [asset.delta_hour_utc for asset in missing] == [
        datetime(2026, 3, 15, 11, tzinfo=UTC),
        datetime(2026, 3, 15, 12, tzinfo=UTC),
    ]
    assert [asset.download_url for asset in missing] == [
        "https://example.test/2026-03-15-11.zip",
        "https://example.test/2026-03-15-12.zip",
    ]


def test_download_asset_keeps_fetch_separate_from_resolution_logic() -> None:
    releases_payload = [{"assets": [_asset("2026-03-15_all_CVEs_at_end_of_day.zip", "https://example.test/base.zip")]}]
    session = _FakeSession(
        [
            _FakeResponse(payload=releases_payload),
            _FakeResponse(content=b"zip-bytes"),
        ]
    )
    client = CveSourceClient(session=session)
    baseline = client.resolve_latest_baseline_asset()

    payload = client.download_asset(baseline)

    assert payload == b"zip-bytes"
    assert session.calls[0][0].endswith("/releases")
    assert session.calls[1][0] == "https://example.test/base.zip"
