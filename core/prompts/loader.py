"""Prompt template loading and rendering.

Consolidates file-based template loading patterns used across the codebase,
including an LRU cache and simple `str.format_map` rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Union


@lru_cache(maxsize=8)
def _read_text_cached(resolved_path: str) -> str:
    """Read a UTF-8 text file from disk with a small global LRU cache."""

    return Path(resolved_path).read_text(encoding="utf-8")


@lru_cache(maxsize=64)
def _read_latest_version_cached(resolved_manifest_path: str) -> str:
    """Read and normalize a `latest.txt` manifest with LRU caching."""

    version = Path(resolved_manifest_path).read_text(encoding="utf-8").strip().lstrip("\ufeff")
    if not version:
        raise ValueError(f"Empty latest.txt in {Path(resolved_manifest_path).parent}")
    return version


PathLike = Union[str, Path]


@dataclass(frozen=True, slots=True)
class TemplateLoader:
    """Load and render prompt templates from a root directory."""

    root_dir: Path

    def load(self, relative_path: PathLike) -> str:
        """Load a template file under `root_dir`."""

        path = (self.root_dir / Path(relative_path)).resolve()
        return _read_text_cached(str(path))

    def render(self, relative_path: PathLike, **context: Any) -> str:
        """Render a template using `str.format_map`."""

        template = self.load(relative_path)
        safe_context: Mapping[str, str] = {
            key: "" if value is None else str(value) for key, value in context.items()
        }
        return template.format_map(safe_context)

    def load_latest_version(self, template_dir: PathLike, filename: str) -> str:
        """Load `filename` from the latest version referenced by `latest.txt`.

        Expects `latest.txt` to live in `root_dir/template_dir/latest.txt` and
        to contain a version folder name (e.g. "v1").
        """

        version = self.get_latest_version(template_dir)
        return self.load(Path(template_dir) / version / filename)

    def get_latest_version(self, template_dir: PathLike) -> str:
        """Return latest version for `template_dir` from cached `latest.txt`."""

        manifest = (self.root_dir / Path(template_dir) / "latest.txt").resolve()
        return _read_latest_version_cached(str(manifest))

    def cache_info(self):
        """Expose cache info for the underlying template file cache."""

        return {
            "template_files": _read_text_cached.cache_info(),
            "latest_manifests": _read_latest_version_cached.cache_info(),
        }

    def cache_clear(self) -> None:
        """Clear underlying template and latest-manifest caches."""

        _read_text_cached.cache_clear()
        _read_latest_version_cached.cache_clear()


__all__ = ["TemplateLoader"]
