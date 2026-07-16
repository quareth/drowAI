"""Generate the repository's evidence-based test inventory.

The generator discovers git-tracked test files, classifies their ownership and
test layer, derives current gate membership from wired release-gate paths and
pytest markers, and writes deterministic CSV and Markdown reports. It does not
claim that an unexecuted test is trusted or assign synthetic timing data.
"""

from __future__ import annotations

import argparse
import ast
import csv
from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV_PATH = REPO_ROOT / "docs/testing/generated/test-inventory.csv"
DEFAULT_SUMMARY_PATH = REPO_ROOT / "docs/testing/generated/test-inventory-summary.md"
DEFAULT_OVERRIDES_PATH = REPO_ROOT / "docs/testing/test-audit-overrides.json"
_RELEASE_PATH_VARIABLES = {
    "quick_backend_paths",
    "main_backend_paths",
    "frontend_contract_paths",
    "fixture_contract_paths",
}
_REGRESSION_MARKERS = {"quick", "main", "nightly"}
_PLAYWRIGHT_TIER_TAGS = {
    "@journey": "e2e-journeys-main-release-ci",
    "@pr-core": "e2e-pr-required-ci",
    "@runtime-local": "e2e-runtime-nightly-release-manual",
}
_FIXTURE_CONTRACT_CI_MEMBERSHIP = "e2e-fixture-contracts-required-pr-ci"


@dataclass(frozen=True)
class InventoryEntry:
    """One tracked test file and its current audit metadata."""

    path: str
    framework: str
    owner: str
    product_area: str
    layer: str
    gate_memberships: tuple[str, ...]
    trust_status: str
    duration_seconds: float | None
    notes: str


def is_test_path(path: str) -> bool:
    """Return whether a tracked path is a collected test module/spec candidate."""

    name = Path(path).name
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith(
        (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
    )


def repository_test_paths(repo_root: Path = REPO_ROOT) -> list[str]:
    """Return sorted tracked and non-ignored working-tree test paths."""

    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    paths = [raw.decode("utf-8", errors="replace") for raw in result.stdout.split(b"\0") if raw]
    return sorted(
        path for path in paths if is_test_path(path) and (repo_root / path).is_file()
    )


def _release_gate_path_sets(repo_root: Path) -> tuple[set[str], set[str]]:
    source = (repo_root / "scripts/run_release_gate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    values: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in _RELEASE_PATH_VARIABLES:
            continue
        literal = ast.literal_eval(node.value)
        values[target.id] = tuple(str(item) for item in literal)

    missing = _RELEASE_PATH_VARIABLES - values.keys()
    if missing:
        raise ValueError(f"Missing release-gate path variables: {sorted(missing)}")

    quick = (
        set(values["quick_backend_paths"])
        | set(values["frontend_contract_paths"])
        | set(values["fixture_contract_paths"])
    )
    main = quick | set(values["main_backend_paths"])
    return quick, main


def _framework(path: str, file_text: str = "") -> str:
    if path.startswith("e2e/") and ".spec." in path:
        return "playwright"
    if path.endswith(".py"):
        return "pytest"
    if "node:test" in file_text:
        return "node:test"
    return "vitest"


def _owner(path: str) -> str:
    root = path.split("/", 1)[0]
    return {
        "backend": "backend",
        "agent": "agent-runtime",
        "client": "frontend",
        "core": "core-platform",
        "e2e": "product-e2e",
        "kali_executor": "kali-executor",
    }.get(root, "cross-system")


def _product_area(path: str) -> str:
    parts = path.split("/")
    if path.startswith("backend/tests/"):
        tail = parts[2:]
        if tail and tail[0] == "services" and len(tail) > 1:
            return f"backend/services/{tail[1]}"
        return f"backend/{tail[0]}" if tail and not tail[0].startswith("test_") else "backend/general"
    if path.startswith("agent/"):
        functional = [part for part in parts[1:-1] if part not in {"tests", "test"}]
        return f"agent/{functional[0]}" if functional else "agent/general"
    if path.startswith("client/src/"):
        tail = [part for part in parts[2:-1] if part != "__tests__"]
        if not tail:
            return "frontend/general"
        if tail[0] in {"components", "features"} and len(tail) > 1:
            return f"frontend/{tail[0]}/{tail[1]}"
        return f"frontend/{tail[0]}"
    if path.startswith("tests/"):
        return f"cross-system/{parts[1]}" if len(parts) > 2 else "cross-system/general"
    if path.startswith("core/"):
        return f"core/{parts[1]}" if len(parts) > 2 else "core/general"
    if path.startswith("e2e/"):
        return "product/e2e"
    if path.startswith("kali_executor/"):
        return "execution/kali-executor"
    return "unmapped"


def _layer(path: str) -> str:
    lowered = path.lower()
    if path.startswith("e2e/probes/"):
        return "integration"
    if path.startswith("e2e/tests/") or "/e2e/" in lowered or "_e2e" in lowered:
        return "end-to-end"
    if "/architecture/" in lowered:
        return "architecture"
    if "/security/" in lowered or "security" in Path(path).name.lower():
        return "security"
    if "/integration/" in lowered or "integration" in Path(path).name.lower():
        return "integration"
    if any(token in lowered for token in ("contract", "protocol", "schema", "boundary", "guardrail")):
        return "contract"
    if path.endswith(".tsx"):
        return "component"
    return "unit"


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _pytest_regression_markers(file_text: str) -> set[str]:
    try:
        tree = ast.parse(file_text)
    except SyntaxError:
        return set()
    prefix = "pytest.mark.regression_"
    markers: set[str] = set()
    for node in ast.walk(tree):
        dotted = _dotted_name(node)
        if not dotted.startswith(prefix):
            continue
        marker = dotted.removeprefix(prefix)
        if marker in _REGRESSION_MARKERS:
            markers.add(marker)
    return markers


def _gate_memberships(
    path: str,
    *,
    file_text: str,
    quick_paths: set[str],
    main_paths: set[str],
    configured_playwright_tags: set[str],
    fixture_contracts_in_ci: bool,
) -> tuple[str, ...]:
    memberships: set[str] = set()
    if path in quick_paths:
        memberships.update({"release-quick-ci", "release-main-manual"})
    elif path in main_paths:
        memberships.add("release-main-manual")

    markers = _pytest_regression_markers(file_text) if path.endswith(".py") else set()
    if "quick" in markers:
        memberships.update({"release-quick-ci", "release-main-manual"})
    if "main" in markers:
        memberships.add("langgraph-main-manual")
    if "nightly" in markers:
        memberships.add("langgraph-nightly-manual")

    if path.startswith("e2e/tests/"):
        for tag in set(re.findall(r"@(journey|pr-core|runtime-local)\b", file_text)):
            normalized_tag = f"@{tag}"
            if normalized_tag in configured_playwright_tags:
                memberships.add(_PLAYWRIGHT_TIER_TAGS[normalized_tag])
    elif path.startswith("e2e/fixtures/") and path.endswith(".test.ts"):
        if fixture_contracts_in_ci:
            memberships.add(_FIXTURE_CONTRACT_CI_MEMBERSHIP)
    elif path.startswith("e2e/probes/") and fixture_contracts_in_ci:
        memberships.add(_FIXTURE_CONTRACT_CI_MEMBERSHIP)

    return tuple(sorted(memberships))


def _trust_status(path: str, memberships: tuple[str, ...]) -> str:
    if "release-quick-ci" in memberships or "e2e-pr-required-ci" in memberships:
        return "trusted-ci-selection"
    if (
        _FIXTURE_CONTRACT_CI_MEMBERSHIP in memberships
        and not path.startswith("e2e/probes/")
    ):
        return "trusted-ci-selection"
    if any(
        membership in memberships
        for membership in (
            "e2e-journeys-main-release-ci",
            _FIXTURE_CONTRACT_CI_MEMBERSHIP,
        )
    ):
        return "candidate-e2e"
    if path.startswith("e2e/tests/"):
        return "environment-dependent"
    if "release-main-manual" in memberships:
        return "curated-manual"
    return "untriaged"


def _configured_playwright_tags(repo_root: Path) -> set[str]:
    """Return tier tags whose destination commands are wired into CI workflows."""

    workflow_commands = {
        "@pr-core": ("e2e-smoke.yml", "npm run test:e2e:pr"),
        "@journey": ("e2e-journeys.yml", "npm run test:e2e:journeys"),
        "@runtime-local": (
            "e2e-runtime-local.yml",
            "npm run test:e2e:runtime:local",
        ),
    }
    workflow_root = repo_root / ".github" / "workflows"
    configured: set[str] = set()
    for tag, (workflow_name, command) in workflow_commands.items():
        workflow = workflow_root / workflow_name
        if workflow.is_file() and command in workflow.read_text(encoding="utf-8"):
            configured.add(tag)
    return configured


def _fixture_contracts_in_ci(repo_root: Path) -> bool:
    """Return whether the complete node:test fixture command is wired into PR CI."""

    workflow = repo_root / ".github" / "workflows" / "e2e-smoke.yml"
    return workflow.is_file() and "npm run test:e2e:fixture-contracts" in workflow.read_text(
        encoding="utf-8"
    )


def build_inventory(
    repo_root: Path = REPO_ROOT,
    *,
    paths: Iterable[str] | None = None,
    timings: Mapping[str, float] | None = None,
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> list[InventoryEntry]:
    """Build deterministic inventory entries for tracked or supplied test paths."""

    quick_paths, main_paths = _release_gate_path_sets(repo_root)
    configured_playwright_tags = _configured_playwright_tags(repo_root)
    fixture_contracts_in_ci = _fixture_contracts_in_ci(repo_root)
    timing_map = timings or {}
    override_map = overrides or {}
    entries: list[InventoryEntry] = []
    for path in sorted(paths if paths is not None else repository_test_paths(repo_root)):
        file_path = repo_root / path
        file_text = file_path.read_text(encoding="utf-8", errors="replace")
        memberships = _gate_memberships(
            path,
            file_text=file_text,
            quick_paths=quick_paths,
            main_paths=main_paths,
            configured_playwright_tags=configured_playwright_tags,
            fixture_contracts_in_ci=fixture_contracts_in_ci,
        )
        notes = ""
        if path.endswith(".py") and _pytest_regression_markers(file_text):
            notes = "Gate selection may apply only to marked tests within this file."
        override = override_map.get(path, {})
        override_notes = str(override.get("notes", "")).strip()
        if override_notes:
            notes = " ".join(part for part in (notes, override_notes) if part)
        duration = override.get("duration_seconds", timing_map.get(path))
        entries.append(
            InventoryEntry(
                path=path,
                framework=_framework(path, file_text),
                owner=str(override.get("owner", _owner(path))),
                product_area=str(override.get("product_area", _product_area(path))),
                layer=str(override.get("layer", _layer(path))),
                gate_memberships=memberships,
                trust_status=str(override.get("trust_status", _trust_status(path, memberships))),
                duration_seconds=None if duration is None else float(duration),
                notes=notes,
            )
        )
    return entries


def render_csv(entries: Iterable[InventoryEntry]) -> str:
    """Render the detailed inventory as stable CSV."""

    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "path",
            "framework",
            "owner",
            "product_area",
            "layer",
            "gate_memberships",
            "trust_status",
            "duration_seconds",
            "notes",
        ]
    )
    for entry in entries:
        writer.writerow(
            [
                entry.path,
                entry.framework,
                entry.owner,
                entry.product_area,
                entry.layer,
                ";".join(entry.gate_memberships),
                entry.trust_status,
                "" if entry.duration_seconds is None else f"{entry.duration_seconds:.3f}",
                entry.notes,
            ]
        )
    return output.getvalue()


def _counts(entries: Iterable[InventoryEntry], attribute: str) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for entry in entries:
        value = str(getattr(entry, attribute))
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def render_summary(entries: list[InventoryEntry]) -> str:
    """Render the concise human-readable inventory summary."""

    measured = sum(entry.duration_seconds is not None for entry in entries)
    lines = [
        "<!-- Generated by scripts/generate_test_inventory.py; do not edit manually. -->",
        "# Generated Test Inventory Summary",
        "",
        f"Repository test files: **{len(entries)}**",
        f"Files with measured duration evidence: **{measured}**",
        "",
        "Trust status describes current evidence, not test quality. `untriaged` means no wired gate or recorded audit evidence currently establishes release ownership.",
        "",
    ]
    for title, attribute in (
        ("Ownership", "owner"),
        ("Test layer", "layer"),
        ("Framework", "framework"),
        ("Trust status", "trust_status"),
    ):
        lines.extend([f"## {title}", "", "| Classification | Files |", "|---|---:|"])
        lines.extend(f"| `{name}` | {count} |" for name, count in _counts(entries, attribute))
        lines.append("")

    membership_counts: dict[str, int] = {}
    for entry in entries:
        for membership in entry.gate_memberships:
            membership_counts[membership] = membership_counts.get(membership, 0) + 1
    lines.extend(["## Current gate membership", "", "| Gate | Files touched |", "|---|---:|"])
    lines.extend(
        f"| `{name}` | {count} |"
        for name, count in sorted(membership_counts.items(), key=lambda item: item[0])
    )
    lines.extend(
        [
            "",
            "The detailed file-level inventory is in [`test-inventory.csv`](test-inventory.csv).",
            "",
        ]
    )
    return "\n".join(lines)


def _load_timings(path: Path | None) -> dict[str, float]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Timing evidence must be a JSON object mapping paths to seconds.")
    return {str(key): float(value) for key, value in payload.items()}


def _load_overrides(path: Path | None) -> dict[str, dict[str, object]]:
    if path is None or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    tests = payload.get("tests") if isinstance(payload, dict) else None
    if not isinstance(tests, dict):
        raise ValueError("Audit overrides must contain a `tests` object.")
    if not all(isinstance(value, dict) for value in tests.values()):
        raise ValueError("Each audit override must be an object.")
    return {str(key): dict(value) for key, value in tests.items()}


def _write_or_check(path: Path, content: str, *, check: bool) -> bool:
    if check:
        return path.is_file() and path.read_text(encoding="utf-8") == content
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--timings", type=Path, help="Optional JSON path-to-seconds evidence map.")
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES_PATH)
    parser.add_argument("--check", action="store_true", help="Fail when generated files are stale.")
    args = parser.parse_args(argv)

    entries = build_inventory(
        timings=_load_timings(args.timings),
        overrides=_load_overrides(args.overrides),
    )
    outputs = {
        args.csv: render_csv(entries),
        args.summary: render_summary(entries),
    }
    stale = [path for path, content in outputs.items() if not _write_or_check(path, content, check=args.check)]
    if stale:
        for path in stale:
            print(f"[test-inventory] stale or missing: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1
    action = "verified" if args.check else "generated"
    print(f"[test-inventory] {action} {len(entries)} repository test files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
