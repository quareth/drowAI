"""Check and build runner-package artifacts with boundary guardrails.

This script validates runner-package roots and import boundaries, and in build
mode writes a tarball containing the declared runner roots plus a thin CLI
entrypoint wrapper.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import tarfile
from pathlib import Path

def _resolve_package_root() -> Path:
    override = os.environ.get("DROWAI_PACKAGE_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _resolve_package_root()
DEFAULT_MANIFEST_PATH = (
    REPO_ROOT / "runtime/manifests/runtime-package-manifest.md"
)
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", flags=re.DOTALL)
_RUNNER_FORBIDDEN_IMPORT_PREFIXES_FALLBACK: tuple[str, ...] = (
    "backend.routers",
    "backend.auth",
    "backend.models",
    "backend.database",
    "backend.services.knowledge",
    "backend.services.artifact",
    "backend.services.terminal",
    "backend.services.llm_provider",
    "backend.services.runtime_provider",
    "backend.services.unified_docker_service",
    "client",
    "server",
)


def _extract_manifest_json(markdown: str) -> dict[str, object]:
    match = _JSON_BLOCK_RE.search(markdown)
    if not match:
        raise ValueError("Manifest JSON block not found in markdown file.")
    return json.loads(match.group(1))


def _load_manifest(manifest_path: Path) -> dict[str, object]:
    payload = _extract_manifest_json(manifest_path.read_text(encoding="utf-8"))
    if "runner_package" not in payload:
        raise ValueError("Manifest missing `runner_package` section.")
    return payload


def _iter_python_files(root_path: Path) -> list[Path]:
    if root_path.is_file() and root_path.suffix == ".py":
        return [root_path]
    if not root_path.is_dir():
        return []
    return sorted(root_path.rglob("*.py"))


def _module_from_path(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _collect_import_candidates(tree: ast.AST) -> list[str]:
    candidates: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            base = node.module or ""
            if base:
                candidates.append(base)
            for alias in node.names:
                candidates.append(f"{base}.{alias.name}" if base else alias.name)
    return candidates


def _runner_roots(payload: dict[str, object]) -> list[str]:
    runner_section = payload.get("runner_package", {})
    if not isinstance(runner_section, dict):
        return []
    roots = runner_section.get("python_roots", [])
    if not isinstance(roots, list):
        return []
    return [str(item) for item in roots]


def _runner_forbidden_import_prefixes(payload: dict[str, object]) -> tuple[str, ...]:
    management_section = payload.get("management_only", {})
    if isinstance(management_section, dict):
        prefixes = management_section.get("python_module_prefixes", [])
        if isinstance(prefixes, list):
            normalized = tuple(str(item) for item in prefixes if str(item).strip())
            if normalized:
                return normalized
    return _RUNNER_FORBIDDEN_IMPORT_PREFIXES_FALLBACK


def _missing_roots(roots: list[str]) -> list[str]:
    missing: list[str] = []
    for root in roots:
        if not (REPO_ROOT / root).exists():
            missing.append(root)
    return missing


def _dependency_violations(
    roots: list[str],
    *,
    forbidden_prefixes: tuple[str, ...],
) -> list[str]:
    violations: list[str] = []
    for root in roots:
        root_path = REPO_ROOT / root
        for py_file in _iter_python_files(root_path):
            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError as exc:
                rel = py_file.relative_to(REPO_ROOT)
                violations.append(f"{rel}:{exc.lineno}: syntax error: {exc.msg}")
                continue

            imports = _collect_import_candidates(tree)
            for imported in imports:
                for forbidden_prefix in forbidden_prefixes:
                    if imported.startswith(forbidden_prefix):
                        rel = py_file.relative_to(REPO_ROOT)
                        violations.append(
                            f"{rel}: forbidden import `{imported}` "
                            f"(runner boundary `{forbidden_prefix}`)"
                        )
                        break
    return violations


def _resolve_runner_version() -> str:
    pyproject_path = REPO_ROOT / "pyproject.toml"
    try:
        content = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(
        r"(?ms)^\[project\]\s+.*?^version\s*=\s*\"([^\"]+)\"",
        content,
    )
    if match and match.group(1).strip():
        return match.group(1).strip()
    return "unknown"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST_PATH),
        help="Path to runtime package manifest markdown file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run check-only diagnostics without writing package artifacts.",
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "dist" / "drowai-runner-package.tar.gz"),
        help="Tarball output path when build mode is enabled.",
    )
    return parser


def _write_runner_tarball(*, roots: list[str], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz") as archive:
        for root in roots:
            root_path = REPO_ROOT / root
            archive.add(root_path, arcname=root, recursive=True)
        cli_entrypoint = (
            "#!/usr/bin/env bash\n"
            'exec python3 -m drowai_runner "$@"\n'
        )
        data = cli_entrypoint.encode("utf-8")
        info = tarfile.TarInfo("bin/drowai-runner")
        info.mode = 0o755
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def main() -> int:
    args = _build_parser().parse_args()
    manifest_path = Path(args.manifest).resolve()
    output_path = Path(args.output).resolve()
    if not manifest_path.exists():
        print(f"[package-runner] ERROR: manifest not found: {manifest_path}")
        return 2

    try:
        payload = _load_manifest(manifest_path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[package-runner] ERROR: invalid manifest: {exc}")
        return 2

    roots = _runner_roots(payload)
    if not roots:
        print("[package-runner] ERROR: runner_package.python_roots is empty.")
        return 2

    if args.check:
        print("[package-runner] CHECK mode enabled (no package output will be written).")
    else:
        print(f"[package-runner] BUILD mode enabled (output: {output_path}).")
    print(f"[package-runner] runner_version={_resolve_runner_version()}")
    print("[package-runner] Runner roots:")
    for root in roots:
        print(f"  - {root}")

    forbidden_prefixes = _runner_forbidden_import_prefixes(payload)
    missing = _missing_roots(roots)
    violations = _dependency_violations(roots, forbidden_prefixes=forbidden_prefixes)

    if missing:
        print("[package-runner] FAIL: missing runner assets:")
        for item in missing:
            print(f"  - {item}")
    if violations:
        print("[package-runner] FAIL: dependency boundary violations:")
        for item in violations:
            print(f"  - {item}")

    if missing or violations:
        return 1

    if not args.check:
        _write_runner_tarball(roots=roots, output_path=output_path)
        print(f"[package-runner] BUILD: wrote runner artifact to {output_path}")
        return 0

    print("[package-runner] PASS: runner packaging checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
