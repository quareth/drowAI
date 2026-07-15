"""Verify runtime-image import boundaries using the package manifest.

The verifier is static-only: it parses Python imports and reports violations
without starting Docker, containers, or any runtime processes.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_shared.runtime_manifest import build_runtime_manifest  # noqa: E402

DEFAULT_MANIFEST_PATH = (
    REPO_ROOT / "runtime/manifests/runtime-package-manifest.md"
)
_JSON_BLOCK_RE = re.compile(
    r"```json\s*(\{.*?\})\s*```",
    flags=re.DOTALL,
)


@dataclass(frozen=True)
class TemporaryException:
    """Allowlisted import violation pending an explicitly owned cleanup task."""

    source_module_prefix: str
    allowed_management_import_prefix: str
    todo: str
    owner: str
    removal_condition: str


@dataclass(frozen=True)
class RuntimePackageManifest:
    """Parsed runtime package manifest contract."""

    runtime_image_python_roots: tuple[str, ...]
    runtime_image_excluded_module_prefixes: tuple[str, ...]
    management_only_module_prefixes: tuple[str, ...]
    temporary_exceptions: tuple[TemporaryException, ...]


def _extract_manifest_json(markdown: str) -> dict[str, object]:
    match = _JSON_BLOCK_RE.search(markdown)
    if not match:
        raise ValueError("Manifest JSON block not found in markdown file.")
    return json.loads(match.group(1))


def _load_manifest(path: Path) -> RuntimePackageManifest:
    payload = _extract_manifest_json(path.read_text(encoding="utf-8"))
    runtime_image = payload.get("runtime_image", {})
    management_only = payload.get("management_only", {})
    raw_exceptions = payload.get("temporary_exceptions", [])

    runtime_roots = tuple(runtime_image.get("python_roots", []))
    excluded_prefixes = tuple(runtime_image.get("excluded_module_prefixes", []))
    management_prefixes = tuple(management_only.get("python_module_prefixes", []))

    exceptions: list[TemporaryException] = []
    for item in raw_exceptions:
        exceptions.append(
            TemporaryException(
                source_module_prefix=item["source_module_prefix"],
                allowed_management_import_prefix=item[
                    "allowed_management_import_prefix"
                ],
                todo=item["todo"],
                owner=item["owner"],
                removal_condition=item["removal_condition"],
            )
        )

    if not runtime_roots:
        raise ValueError("Manifest runtime_image.python_roots is empty.")
    if not management_prefixes:
        raise ValueError("Manifest management_only.python_module_prefixes is empty.")

    return RuntimePackageManifest(
        runtime_image_python_roots=runtime_roots,
        runtime_image_excluded_module_prefixes=excluded_prefixes,
        management_only_module_prefixes=management_prefixes,
        temporary_exceptions=tuple(exceptions),
    )


def _iter_python_files(runtime_roots: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for root in runtime_roots:
        root_path = REPO_ROOT / root
        if root_path.is_file() and root_path.suffix == ".py":
            files.append(root_path)
            continue
        if root_path.is_dir():
            files.extend(sorted(root_path.rglob("*.py")))
    return sorted(set(files))


def _module_from_path(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _collect_import_candidates(tree: ast.AST) -> list[str]:
    candidates: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                candidates.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            base = node.module or ""
            if base:
                candidates.append(base)
            for alias in node.names:
                if base:
                    candidates.append(f"{base}.{alias.name}")
    return candidates


def _is_exception(
    *,
    source_module: str,
    imported_module: str,
    exceptions: tuple[TemporaryException, ...],
) -> bool:
    for exc in exceptions:
        if source_module.startswith(exc.source_module_prefix) and imported_module.startswith(
            exc.allowed_management_import_prefix
        ):
            return True
    return False


def _find_violations(manifest: RuntimePackageManifest) -> list[str]:
    violations: list[str] = []
    runtime_files = _iter_python_files(manifest.runtime_image_python_roots)

    for file_path in runtime_files:
        source = file_path.read_text(encoding="utf-8-sig")
        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:
            rel = file_path.relative_to(REPO_ROOT)
            violations.append(f"{rel}:{exc.lineno}: syntax error: {exc.msg}")
            continue

        source_module = _module_from_path(file_path)
        if any(
            source_module.startswith(prefix)
            for prefix in manifest.runtime_image_excluded_module_prefixes
        ):
            continue
        imports = _collect_import_candidates(tree)
        for imported in imports:
            for forbidden_prefix in manifest.management_only_module_prefixes:
                if not imported.startswith(forbidden_prefix):
                    continue
                if _is_exception(
                    source_module=source_module,
                    imported_module=imported,
                    exceptions=manifest.temporary_exceptions,
                ):
                    continue
                rel = file_path.relative_to(REPO_ROOT)
                violations.append(
                    f"{rel}: forbidden import `{imported}` "
                    f"(matches management-only prefix `{forbidden_prefix}`)"
                )
                break
    return violations


def _validate_runtime_manifest_contract() -> list[str]:
    """Validate required runtime-manifest fields for daemon version probing."""
    payload = build_runtime_manifest().to_dict()
    required_fields = (
        "runtime_contract_version",
        "source_revision",
        "supported_tool_families",
        "file_comm_schema_version",
        "semantic_schema_versions",
        "workspace_layout_version",
    )
    errors: list[str] = []
    for field_name in required_fields:
        value = payload.get(field_name)
        if value in ("", None, [], {}):
            errors.append(f"runtime manifest missing `{field_name}`")
    return errors


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST_PATH),
        help="Path to runtime package manifest markdown file.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"[verify-runtime-package] ERROR: manifest not found: {manifest_path}")
        return 2

    try:
        manifest = _load_manifest(manifest_path)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"[verify-runtime-package] ERROR: invalid manifest: {exc}")
        return 2

    runtime_manifest = build_runtime_manifest()
    print(
        "[verify-runtime-package] runtime_contract_version="
        f"{runtime_manifest.runtime_contract_version}"
    )
    print("[verify-runtime-package] included_runtime_roots:")
    for root in manifest.runtime_image_python_roots:
        print(f"  - {root}")

    violations = _find_violations(manifest)
    violations.extend(_validate_runtime_manifest_contract())
    if violations:
        print("[verify-runtime-package] FAIL: runtime package verification violations:")
        for item in violations:
            print(f"  - {item}")
        return 1

    print("[verify-runtime-package] PASS: no forbidden management-only imports found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
