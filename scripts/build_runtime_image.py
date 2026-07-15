"""Check and build runtime-image packaging inputs with boundary guardrails.

Build and publish both architecture tags (same Kali packages + execution-plane layers):

    python scripts/build_runtime_image.py --check
    python scripts/build_runtime_image.py --arch arm64 --push
    python scripts/build_runtime_image.py --arch amd64 --push
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_shared.runtime_image_contract import (  # noqa: E402
    normalize_runtime_arch,
    runtime_base_image_for_arch,
    runtime_image_for_arch,
    runtime_platform_for_arch,
)
from runtime_shared.runtime_manifest import build_runtime_manifest  # noqa: E402

DEFAULT_MANIFEST_PATH = (
    REPO_ROOT / "runtime/manifests/runtime-package-manifest.md"
)
DEFAULT_DOCKERFILE_PATH = REPO_ROOT / "runtime/image/Dockerfile"
DEFAULT_KALI_BASE_DOCKERFILE_PATH = REPO_ROOT / "runtime/image/Dockerfile.kali-base"
DEFAULT_RUNTIME_REQUIREMENTS_PATH = REPO_ROOT / "runtime/image/runtime-requirements.txt"
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", flags=re.DOTALL)
_REQ_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+")
_RUNTIME_ROOT_PREFIX = "/opt/drowai/runtime/"
_RUNTIME_PYTHON_ROOT_PREFIX = "/opt/drowai/runtime/python/"
_DISALLOWED_RUNTIME_SOURCE_PREFIXES = (
    "backend/",
    "client/",
    "server/",
    "agent/tools",
    "agent/tool_runtime",
    "agent/graph",
    "agent/semantic",
    "agent/models.py",
    "agent/execution_strategy.py",
)

FORBIDDEN_RUNTIME_DEPENDENCIES = {
    "alembic",
    "anthropic",
    "fastapi",
    "langgraph",
    "langgraph-checkpoint-postgres",
    "langgraph-checkpoint-sqlite",
    "openai",
    "passlib",
    "python-jose",
    "sqlalchemy",
    "uvicorn",
}


def _extract_manifest_json(markdown: str) -> dict[str, object]:
    match = _JSON_BLOCK_RE.search(markdown)
    if not match:
        raise ValueError("Manifest JSON block not found in markdown file.")
    return json.loads(match.group(1))


def _load_manifest(manifest_path: Path) -> dict[str, object]:
    payload = _extract_manifest_json(manifest_path.read_text(encoding="utf-8"))
    if "runtime_image" not in payload:
        raise ValueError("Manifest missing `runtime_image` section.")
    return payload


def _runtime_roots(payload: dict[str, object]) -> list[str]:
    runtime_image = payload.get("runtime_image", {})
    if not isinstance(runtime_image, dict):
        return []
    roots = runtime_image.get("python_roots", [])
    if not isinstance(roots, list):
        return []
    return [str(item) for item in roots]


def _required_entrypoint_sources(payload: dict[str, object]) -> list[str]:
    runtime_image = payload.get("runtime_image", {})
    if not isinstance(runtime_image, dict):
        return []
    entries = runtime_image.get("required_entrypoint_sources", [])
    if not isinstance(entries, list):
        return []
    return [str(item) for item in entries]


def _missing_roots(roots: list[str]) -> list[str]:
    missing: list[str] = []
    for root in roots:
        if not (REPO_ROOT / root).exists():
            missing.append(root)
    return missing


def _directory_runtime_roots(payload: dict[str, object]) -> list[str]:
    """Return runtime-image roots that are directories instead of explicit files."""
    return sorted(
        root
        for root in _runtime_roots(payload)
        if (REPO_ROOT / root).is_dir()
    )


def _normalize_source_path(source: str) -> str:
    normalized = source.strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


@dataclass(frozen=True, slots=True)
class RuntimeCopySource:
    source: str
    destination: str


def _runtime_copy_sources(dockerfile_path: Path) -> list[RuntimeCopySource]:
    sources: list[RuntimeCopySource] = []
    for raw_line in dockerfile_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith("COPY "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) < 3:
            continue
        destination = parts[-1]
        if not destination.startswith(_RUNTIME_ROOT_PREFIX):
            continue
        for source in parts[1:-1]:
            if source.startswith("--"):
                continue
            sources.append(
                RuntimeCopySource(
                    source=_normalize_source_path(source),
                    destination=destination,
                )
            )
    deduped = {(entry.source, entry.destination): entry for entry in sources}
    return sorted(deduped.values(), key=lambda entry: (entry.source, entry.destination))


def _runtime_python_copy_sources(dockerfile_path: Path) -> list[str]:
    return sorted(
        {
            entry.source
            for entry in _runtime_copy_sources(dockerfile_path)
            if entry.destination.startswith(_RUNTIME_PYTHON_ROOT_PREFIX)
        }
    )


def _directory_runtime_python_copy_sources(dockerfile_path: Path) -> list[str]:
    """Return Docker runtime Python COPY sources that recursively copy directories."""
    return sorted(
        source
        for source in _runtime_python_copy_sources(dockerfile_path)
        if (REPO_ROOT / source).is_dir()
    )


def _disallowed_runtime_copy_sources(copy_sources: list[RuntimeCopySource]) -> list[str]:
    disallowed: set[str] = set()
    for entry in copy_sources:
        if entry.source.startswith(_DISALLOWED_RUNTIME_SOURCE_PREFIXES):
            disallowed.add(entry.source)
    return sorted(disallowed)


def _expected_runtime_python_sources(payload: dict[str, object]) -> list[str]:
    expected = {
        _normalize_source_path(item)
        for item in _runtime_roots(payload) + _required_entrypoint_sources(payload)
    }
    return sorted(expected)


def _run_runtime_boundary_check(manifest_path: Path) -> tuple[int, str]:
    verifier = REPO_ROOT / "scripts/verify_runtime_package.py"
    proc = subprocess.run(
        [
            sys.executable,
            str(verifier),
            "--manifest",
            str(manifest_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def _requirement_name(requirement_line: str) -> str | None:
    line = requirement_line.strip()
    if not line or line.startswith("#"):
        return None
    candidate = re.split(r"[<>=!~;\\[]", line, maxsplit=1)[0].strip()
    match = _REQ_NAME_RE.match(candidate)
    if not match:
        return None
    return match.group(0).lower().replace("_", "-")


def _forbidden_runtime_requirements(requirements_path: Path) -> list[str]:
    if not requirements_path.exists():
        return []
    forbidden: list[str] = []
    for raw_line in requirements_path.read_text(encoding="utf-8").splitlines():
        name = _requirement_name(raw_line)
        if not name:
            continue
        if name in FORBIDDEN_RUNTIME_DEPENDENCIES:
            forbidden.append(name)
    return sorted(set(forbidden))


def _run_docker_buildx(
    *,
    dockerfile_path: Path,
    image_tag: str,
    platform: str,
    build_args: dict[str, str] | None = None,
    push: bool = False,
) -> tuple[int, str]:
    command = [
        "docker",
        "buildx",
        "build",
        "-f",
        str(dockerfile_path),
        "--platform",
        platform,
        "-t",
        image_tag,
    ]
    for key, value in (build_args or {}).items():
        command.extend(["--build-arg", f"{key}={value}"])
    if push:
        command.append("--push")
    else:
        command.append("--load")
    command.append(str(REPO_ROOT))
    print(f"[build-runtime-image] Running: {' '.join(command)}")
    proc = subprocess.run(command, cwd=str(REPO_ROOT), check=False)
    if proc.returncode == 0:
        return 0, f"built {image_tag}"
    return proc.returncode, f"docker buildx failed for {image_tag}"


def _build_kali_base_image(*, arch: str, push: bool) -> tuple[int, str]:
    base_tag = runtime_base_image_for_arch(arch)
    platform = runtime_platform_for_arch(arch)
    if not DEFAULT_KALI_BASE_DOCKERFILE_PATH.is_file():
        return 1, f"missing kali base Dockerfile: {DEFAULT_KALI_BASE_DOCKERFILE_PATH}"
    print(f"[build-runtime-image] Building Kali base {base_tag} ({platform})...")
    return _run_docker_buildx(
        dockerfile_path=DEFAULT_KALI_BASE_DOCKERFILE_PATH,
        image_tag=base_tag,
        platform=platform,
        push=push,
    )


def _build_runtime_image(
    *,
    image_tag: str,
    dockerfile_path: Path,
    base_image: str,
    platform: str,
    push: bool,
) -> tuple[int, str]:
    print(f"[build-runtime-image] Building runtime {image_tag} ({platform})...")
    return _run_docker_buildx(
        dockerfile_path=dockerfile_path,
        image_tag=image_tag,
        platform=platform,
        build_args={"KALI_BASE_IMAGE": base_image},
        push=push,
    )


def _build_architecture(
    *,
    arch: str,
    dockerfile_path: Path,
    push: bool,
    skip_kali_base: bool,
    base_image_override: str,
    image_tag_override: str,
) -> int:
    base_image = base_image_override or runtime_base_image_for_arch(arch)
    image_tag = image_tag_override or runtime_image_for_arch(arch)
    platform = runtime_platform_for_arch(arch)

    if not skip_kali_base:
        code, message = _build_kali_base_image(arch=arch, push=push)
        if code != 0:
            print(f"[build-runtime-image] FAIL: {message}")
            return code
        print(f"[build-runtime-image] PASS: {message}")

    code, message = _build_runtime_image(
        image_tag=image_tag,
        dockerfile_path=dockerfile_path,
        base_image=base_image,
        platform=platform,
        push=push,
    )
    if code != 0:
        print(f"[build-runtime-image] FAIL: {message}")
        return code
    print(f"[build-runtime-image] PASS: {message}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST_PATH),
        help="Path to runtime package manifest markdown file.",
    )
    parser.add_argument(
        "--arch",
        choices=("arm64", "amd64", "all"),
        default="",
        help="Target CPU architecture. Builds matching {arch}-base then {arch}-runtime.",
    )
    parser.add_argument(
        "--image-tag",
        default="",
        help="Override runtime output tag (default: drowai/kali-pentesting:{arch}-runtime).",
    )
    parser.add_argument(
        "--dockerfile",
        default=str(DEFAULT_DOCKERFILE_PATH),
        help="Runtime overlay Dockerfile path.",
    )
    parser.add_argument(
        "--runtime-requirements",
        default=str(DEFAULT_RUNTIME_REQUIREMENTS_PATH),
        help="Runtime-image Python dependency lock file.",
    )
    parser.add_argument(
        "--base-image",
        default="",
        help="Override Kali base build-arg (default: drowai/kali-pentesting:{arch}-base).",
    )
    parser.add_argument(
        "--skip-kali-base",
        action="store_true",
        help="Skip Dockerfile.kali-base and build only the runtime overlay layer.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push built tags to the registry via buildx (required for multi-arch publish).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run check-only diagnostics without building a Docker image.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    manifest_path = Path(args.manifest).resolve()
    dockerfile_path = Path(args.dockerfile).resolve()
    runtime_requirements_path = Path(args.runtime_requirements).resolve()

    if args.check:
        print("[build-runtime-image] CHECK mode enabled (no image build will be run).")
    else:
        print("[build-runtime-image] BUILD mode enabled.")
    print(f"[build-runtime-image] image_tag={args.image_tag or '<arch>-runtime'}")
    print(f"[build-runtime-image] base_image={args.base_image or '<arch>-base'}")
    print(f"[build-runtime-image] dockerfile={dockerfile_path}")
    print(
        "[build-runtime-image] runtime_requirements="
        f"{runtime_requirements_path}"
    )

    if not manifest_path.exists():
        print(f"[build-runtime-image] ERROR: manifest not found: {manifest_path}")
        return 2

    try:
        payload = _load_manifest(manifest_path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[build-runtime-image] ERROR: invalid manifest: {exc}")
        return 2

    roots = _runtime_roots(payload)
    if not roots:
        print("[build-runtime-image] ERROR: runtime_image.python_roots is empty.")
        return 2
    runtime_manifest = build_runtime_manifest()
    print(
        "[build-runtime-image] runtime_contract_version="
        f"{runtime_manifest.runtime_contract_version}"
    )
    print("[build-runtime-image] included_runtime_roots:")
    for root in roots:
        print(f"  - {root}")

    required_entrypoints = _required_entrypoint_sources(payload)
    if not required_entrypoints:
        print(
            "[build-runtime-image] ERROR: "
            "runtime_image.required_entrypoint_sources is empty."
        )
        return 2

    missing = _missing_roots(roots)
    missing.extend(_missing_roots(required_entrypoints))
    directory_runtime_roots = _directory_runtime_roots(payload)
    parity_missing: list[str] = []
    parity_unexpected: list[str] = []
    disallowed_runtime_sources: list[str] = []
    directory_runtime_copy_sources: list[str] = []
    if not dockerfile_path.exists():
        print(f"[build-runtime-image] FAIL: missing Dockerfile: {dockerfile_path}")
        missing.append(str(dockerfile_path.relative_to(REPO_ROOT)))
    else:
        runtime_copy_sources = _runtime_copy_sources(dockerfile_path)
        disallowed_runtime_sources = _disallowed_runtime_copy_sources(runtime_copy_sources)
        directory_runtime_copy_sources = _directory_runtime_python_copy_sources(
            dockerfile_path
        )
        expected_copy_sources = set(_expected_runtime_python_sources(payload))
        actual_copy_sources = set(_runtime_python_copy_sources(dockerfile_path))
        parity_missing = sorted(expected_copy_sources - actual_copy_sources)
        parity_unexpected = sorted(actual_copy_sources - expected_copy_sources)
        if parity_missing:
            print(
                "[build-runtime-image] FAIL: Dockerfile is missing runtime python COPY "
                "sources required by manifest:"
            )
            for item in parity_missing:
                print(f"  - {item}")
        if parity_unexpected:
            print(
                "[build-runtime-image] FAIL: Dockerfile copies runtime python sources "
                "outside manifest-approved roots:"
            )
            for item in parity_unexpected:
                print(f"  - {item}")
        if disallowed_runtime_sources:
            print(
                "[build-runtime-image] FAIL: runtime image COPY sources include "
                "management-only roots:"
            )
            for item in disallowed_runtime_sources:
                print(f"  - {item}")
        if directory_runtime_copy_sources:
            print(
                "[build-runtime-image] FAIL: runtime image Python COPY sources "
                "must be explicit files, not directories:"
            )
            for item in directory_runtime_copy_sources:
                print(f"  - {item}")
    if directory_runtime_roots:
        print(
            "[build-runtime-image] FAIL: runtime_image.python_roots must list "
            "explicit files, not directories:"
        )
        for item in directory_runtime_roots:
            print(f"  - {item}")
    if not runtime_requirements_path.exists():
        print(
            "[build-runtime-image] FAIL: missing runtime requirements file: "
            f"{runtime_requirements_path}"
        )
        missing.append(str(runtime_requirements_path.relative_to(REPO_ROOT)))

    if missing:
        print("[build-runtime-image] FAIL: missing runtime assets:")
        for item in missing:
            print(f"  - {item}")

    forbidden_runtime_requirements = _forbidden_runtime_requirements(
        runtime_requirements_path
    )
    if forbidden_runtime_requirements:
        print(
            "[build-runtime-image] FAIL: runtime requirements include "
            "management-only dependencies:"
        )
        for package_name in forbidden_runtime_requirements:
            print(f"  - {package_name}")

    verify_code, verify_output = _run_runtime_boundary_check(manifest_path)
    if verify_output:
        print(verify_output)

    if (
        missing
        or directory_runtime_roots
        or parity_missing
        or parity_unexpected
        or disallowed_runtime_sources
        or directory_runtime_copy_sources
        or verify_code != 0
        or forbidden_runtime_requirements
    ):
        return 1

    if args.check:
        print("[build-runtime-image] PASS: runtime image checks passed.")
        return 0

    if not str(args.arch).strip():
        print("[build-runtime-image] ERROR: --arch is required for build mode.")
        return 2
    raw_arch = str(args.arch).strip()
    if raw_arch == "all":
        if str(args.base_image).strip() or str(args.image_tag).strip():
            print(
                "[build-runtime-image] ERROR: --base-image/--image-tag overrides "
                "are ambiguous with --arch all. Build each architecture separately "
                "when overriding tags."
            )
            return 2
        for arch in ("arm64", "amd64"):
            code = _build_architecture(
                arch=arch,
                dockerfile_path=dockerfile_path,
                push=bool(args.push),
                skip_kali_base=bool(args.skip_kali_base),
                base_image_override="",
                image_tag_override="",
            )
            if code != 0:
                return code
        return 0

    try:
        arch = normalize_runtime_arch(raw_arch)
    except ValueError as exc:
        print(f"[build-runtime-image] ERROR: {exc}")
        return 2

    return _build_architecture(
        arch=arch,
        dockerfile_path=dockerfile_path,
        push=bool(args.push),
        skip_kali_base=bool(args.skip_kali_base),
        base_image_override=str(args.base_image).strip(),
        image_tag_override=str(args.image_tag).strip(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
