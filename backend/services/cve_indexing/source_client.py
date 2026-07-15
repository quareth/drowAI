"""HTTP source client for CVE baseline and delta release ZIP assets.

Scope:
- Resolves latest baseline and same-day hourly delta assets from official releases.
- Downloads ZIP bytes using `requests` without coupling to parser/upsert logic.

Boundary:
- Does not parse ZIP contents or write to the database.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Iterable

import requests
from backend.services.cve_indexing.primitives import to_utc_hour

DEFAULT_RELEASES_API = "https://api.github.com/repos/CVEProject/cvelistV5/releases"
DEFAULT_TOKEN_ENV_VAR = "GITHUB_TOKEN"
DEFAULT_TIMEOUT_SECONDS = 30.0

_DATE_PATTERN = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")
_DELTA_HOUR_PATTERN = re.compile(r"(?i)at[_\-]?(?P<hour>[01]\d|2[0-3])(?:[.:]?00)?(?:z|utc)")


class CveSourceClientError(RuntimeError):
    """Base error for CVE source-client failures."""


class CveSourceAssetNotFoundError(CveSourceClientError):
    """Raised when required baseline/delta release assets cannot be resolved."""


@dataclass(slots=True, frozen=True)
class CveSourceAsset:
    """Normalized source release asset for one baseline or delta ZIP."""

    name: str
    download_url: str
    published_at: datetime
    baseline_day: date
    delta_hour_utc: datetime | None = None


class CveSourceClient:
    """Client that queries GitHub releases and downloads CVE ZIP assets."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        releases_api_url: str = DEFAULT_RELEASES_API,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        token_env_var: str = DEFAULT_TOKEN_ENV_VAR,
    ) -> None:
        self._session = session or requests.Session()
        self._releases_api_url = releases_api_url
        self._timeout_seconds = timeout_seconds
        self._token = os.getenv(token_env_var, "").strip() or None

    def resolve_latest_baseline_asset(self) -> CveSourceAsset:
        """Return the newest baseline ZIP asset available from releases."""
        candidates: list[CveSourceAsset] = []
        for raw_asset in self._iter_release_assets():
            candidate = self._parse_baseline_asset(raw_asset)
            if candidate is not None:
                candidates.append(candidate)

        if not candidates:
            raise CveSourceAssetNotFoundError("No baseline ZIP asset found in source releases.")

        return max(candidates, key=lambda item: (item.baseline_day, item.published_at))

    def resolve_missing_delta_assets(
        self,
        *,
        baseline_day: date,
        applied_hours: Iterable[datetime] = (),
    ) -> tuple[CveSourceAsset, ...]:
        """Return missing same-day hourly delta assets sorted by hour."""
        assets_by_hour: dict[datetime, CveSourceAsset] = {}
        for raw_asset in self._iter_release_assets():
            parsed = self._parse_delta_asset(raw_asset)
            if parsed is None or parsed.baseline_day != baseline_day or parsed.delta_hour_utc is None:
                continue
            current = assets_by_hour.get(parsed.delta_hour_utc)
            if current is None or parsed.published_at > current.published_at:
                assets_by_hour[parsed.delta_hour_utc] = parsed

        if not assets_by_hour:
            return ()

        applied_hours_utc = set()
        for hour in applied_hours:
            normalized = to_utc_hour(hour)
            if normalized.date() == baseline_day:
                applied_hours_utc.add(normalized)
        missing_hours = sorted(hour for hour in assets_by_hour if hour not in applied_hours_utc)
        return tuple(assets_by_hour[hour] for hour in missing_hours)

    def download_asset(self, asset: CveSourceAsset) -> bytes:
        """Download a release asset and return raw ZIP bytes."""
        response = self._session.get(
            asset.download_url,
            headers=self._build_headers(),
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return bytes(response.content)

    def _iter_release_assets(self) -> Iterable[dict[str, Any]]:
        releases = self._get_releases_payload()
        for release in releases:
            assets = release.get("assets")
            if not isinstance(assets, list):
                continue
            for asset in assets:
                if isinstance(asset, dict):
                    yield asset

    def _get_releases_payload(self) -> list[dict[str, Any]]:
        response = self._session.get(
            self._releases_api_url,
            headers=self._build_headers(),
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise CveSourceClientError("Unexpected releases response shape from source API.")
        return [item for item in payload if isinstance(item, dict)]

    def _build_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _parse_baseline_asset(self, asset: dict[str, Any]) -> CveSourceAsset | None:
        name = str(asset.get("name", "")).strip()
        if not name.lower().endswith(".zip"):
            return None
        lowered = name.lower()
        if "delta" in lowered:
            return None
        if "all" not in lowered and "baseline" not in lowered:
            return None

        baseline_day = self._extract_date(name)
        if baseline_day is None:
            return None
        return CveSourceAsset(
            name=name,
            download_url=self._extract_download_url(asset),
            published_at=self._extract_published_at(asset),
            baseline_day=baseline_day,
            delta_hour_utc=None,
        )

    def _parse_delta_asset(self, asset: dict[str, Any]) -> CveSourceAsset | None:
        name = str(asset.get("name", "")).strip()
        if not name.lower().endswith(".zip"):
            return None
        if "delta" not in name.lower():
            return None

        baseline_day = self._extract_date(name)
        hour = self._extract_delta_hour(name)
        if baseline_day is None or hour is None:
            return None

        return CveSourceAsset(
            name=name,
            download_url=self._extract_download_url(asset),
            published_at=self._extract_published_at(asset),
            baseline_day=baseline_day,
            delta_hour_utc=datetime(
                baseline_day.year,
                baseline_day.month,
                baseline_day.day,
                hour,
                tzinfo=UTC,
            ),
        )

    @staticmethod
    def _extract_date(name: str) -> date | None:
        match = _DATE_PATTERN.search(name)
        if match is None:
            return None
        try:
            return date.fromisoformat(match.group("date"))
        except ValueError:
            return None

    @staticmethod
    def _extract_delta_hour(name: str) -> int | None:
        match = _DELTA_HOUR_PATTERN.search(name)
        if match is None:
            return None
        return int(match.group("hour"))

    @staticmethod
    def _extract_download_url(asset: dict[str, Any]) -> str:
        for key in ("browser_download_url", "url"):
            value = asset.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise CveSourceClientError("Release asset is missing a download URL.")

    @staticmethod
    def _extract_published_at(asset: dict[str, Any]) -> datetime:
        raw = asset.get("updated_at") or asset.get("created_at")
        if not isinstance(raw, str) or not raw.strip():
            return datetime.min.replace(tzinfo=UTC)
        value = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.min.replace(tzinfo=UTC)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

__all__ = [
    "CveSourceAsset",
    "CveSourceAssetNotFoundError",
    "CveSourceClient",
    "CveSourceClientError",
]
