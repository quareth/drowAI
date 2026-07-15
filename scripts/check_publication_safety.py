"""Validate that a release snapshot excludes local and sensitive repository state.

This module checks tracked paths, generalized workflow-state examples, and
representative ignore-rule sentinels. It reports names and policy reasons only;
it never reads or prints secret-file contents.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PREFIXES = (
    ".drowai-local/",
    ".drowai-runner/",
    ".drowai-runner-cloud/",
    ".playwright-cli/",
    ".tmp/",
    "agent/durable_knowledge/",
    "agent/management_state/",
    "agent/workspaces/",
    "artifacts/",
    "backend/management_state/",
    "output/",
    "wordlists/",
    "workspace/",
)
LOCAL_CONFIG_BASENAMES = {
    ".DS_Store",
    ".encryption_key",
    ".envrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
}
SENSITIVE_SUFFIXES = {
    ".cred",
    ".crt",
    ".key",
    ".ovpn",
    ".p12",
    ".pcap",
    ".pcapng",
    ".pem",
    ".pfx",
    ".secret",
    ".token",
}
PUBLIC_SENSITIVE_FIXTURE_PATHS = {
    "tests/fixtures/vpn/invalid-public-endpoint.ovpn",
}
IGNORE_SENTINELS = (
    ".aws/credentials",
    ".azure/accessTokens.json",
    ".direnv/cache",
    ".docker/config.json",
    ".env.staging",
    ".envrc",
    ".kube/config",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "capture.pcap",
    "config/.env.production",
    "credentials/local.json",
    "profile.ovpn",
)
ACTIVE_STATE_PATTERN = re.compile(r"^(?:\.codex|\.cursor)/agents/.*-state\.md$")
STATE_EXAMPLE_PATTERN = re.compile(
    r"^(?:\.codex|\.cursor)/agents/.*state.*\.example\.md$"
)
CONCRETE_TIMESTAMP_PATTERN = re.compile(r"\b20\d{2}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b")


def tracked_path_violation(path: str) -> str | None:
    """Return the publication policy violated by a tracked path, if any."""

    normalized = path.replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    basename = pure_path.name

    if normalized.startswith(FORBIDDEN_PREFIXES):
        return "runtime or generated state"
    if ACTIVE_STATE_PATTERN.fullmatch(normalized):
        return "live agent workflow state"
    if basename == ".env" or basename.startswith(".env."):
        return "environment override"
    if basename in LOCAL_CONFIG_BASENAMES:
        return "local credential or host configuration"
    if normalized in PUBLIC_SENSITIVE_FIXTURE_PATHS:
        return None
    if pure_path.suffix.lower() in SENSITIVE_SUFFIXES:
        return "credential, capture, or private-target material"
    if any(part in {".aws", ".azure", ".kube", "credentials"} for part in pure_path.parts):
        return "local cloud or credential directory"
    if normalized == ".docker/config.json":
        return "local container registry credentials"
    return None


def state_example_violations(path: str, content: str) -> list[str]:
    """Return public-safety problems found in a workflow-state example."""

    if not STATE_EXAMPLE_PATTERN.fullmatch(path):
        return []

    violations: list[str] = []
    if CONCRETE_TIMESTAMP_PATTERN.search(content):
        violations.append("contains a concrete timestamp")
    if any(marker in content for marker in ("/Users/", "C:\\Users\\", ".tmp/")):
        violations.append("contains a local filesystem path")
    if path.endswith("/implementation-state.example.md") and any(
        marker in content for marker in ("docs/plans/", "docs/plan/", "docs/refactor/")
    ):
        violations.append("contains a historical implementation guide path")
    return violations


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ("git", *args),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    """Run publication checks and return a process-compatible exit status."""

    failures: list[str] = []
    tracked_paths = _git_lines("ls-files")

    for path in tracked_paths:
        if reason := tracked_path_violation(path):
            failures.append(f"tracked path: {path} ({reason})")
        if STATE_EXAMPLE_PATTERN.fullmatch(path):
            content = (REPO_ROOT / path).read_text(encoding="utf-8")
            failures.extend(
                f"state example: {path} ({reason})"
                for reason in state_example_violations(path, content)
            )

    failures.extend(
        f"tracked path is ignored: {path}"
        for path in _git_lines("ls-files", "-ci", "--exclude-standard")
    )

    for sentinel in IGNORE_SENTINELS:
        result = subprocess.run(
            ("git", "check-ignore", "-q", sentinel),
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode != 0:
            failures.append(f"ignore rule missing: {sentinel}")

    if failures:
        print("[publication-safety] FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"[publication-safety] OK: {len(tracked_paths)} tracked paths checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
