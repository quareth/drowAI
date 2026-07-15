"""Run cutover parity-certification checks and optional reused target commands.

This script is the cutover certification entrypoint. It renders deterministic
JSON/Markdown reports, inventories reused certification assets, and exits
non-zero when required coverage gaps, missing reused targets, or command
failures are detected.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.cutover.parity_matrix import (  # noqa: E402
    CutoverCertificationTarget,
    build_cutover_certification_report,
)


@dataclass(frozen=True)
class _CommandResult:
    target_id: str
    command: str
    returncode: int
    output: str


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=("quick", "main"),
        default="quick",
        help="Certification tier (`quick` for local deterministic inventory, `main` for stronger backend/object-store readiness).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "artifacts" / "cutover"),
        help="Directory where JSON/Markdown report artifacts will be written.",
    )
    parser.add_argument(
        "--run-targets",
        action="store_true",
        help="Execute reused target commands in addition to inventory/report generation.",
    )
    return parser


def _target_in_tier(target: CutoverCertificationTarget, tier: str) -> bool:
    if target.tier == "both":
        return True
    return target.tier == tier


def _resolve_command(command: str) -> list[str]:
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError("Target command cannot be empty.")

    if tokens[0] == "pytest":
        return [sys.executable, "-m", "pytest", *tokens[1:]]
    if tokens[0] == "python":
        return [sys.executable, *tokens[1:]]
    return tokens


def _run_command(command: str) -> tuple[int, str]:
    resolved = _resolve_command(command)
    proc = subprocess.run(
        resolved,
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output.strip()


def _main_tier_readiness_errors() -> list[str]:
    errors: list[str] = []
    database_url = (str(os.environ.get("DATABASE_URL", "")).strip() or "").lower()
    object_store_backend = (
        str(os.environ.get("DATA_PLANE_OBJECT_STORE_BACKEND", "local")).strip() or "local"
    ).lower()

    if not database_url:
        errors.append("DATABASE_URL is unset for `--tier main`.")
    elif database_url.startswith("sqlite"):
        errors.append("`--tier main` requires a configured non-SQLite DATABASE_URL.")

    if object_store_backend == "local":
        errors.append("`--tier main` requires non-local DATA_PLANE_OBJECT_STORE_BACKEND.")

    return errors


def main() -> int:
    args = _build_parser().parse_args()
    report = build_cutover_certification_report()
    payload = report.to_dict(repo_root=REPO_ROOT)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    json_report_path = output_dir / f"cutover-certification-{args.tier}.json"
    markdown_report_path = output_dir / f"cutover-certification-{args.tier}.md"

    json_report_path.write_text(report.to_json(repo_root=REPO_ROOT), encoding="utf-8")
    markdown_report_path.write_text(report.to_markdown(repo_root=REPO_ROOT), encoding="utf-8")

    tier_readiness_errors: list[str] = []
    if args.tier == "main":
        tier_readiness_errors = _main_tier_readiness_errors()

    command_results: list[_CommandResult] = []
    command_failures: list[str] = []
    if args.run_targets:
        for target in report.reused_targets:
            if not _target_in_tier(target, args.tier):
                continue
            returncode, output = _run_command(target.command)
            command_results.append(
                _CommandResult(
                    target_id=target.id,
                    command=target.command,
                    returncode=returncode,
                    output=output,
                )
            )
            if returncode != 0:
                command_failures.append(target.id)

    blocking_missing_workflows = payload["blocking_missing_workflows"]
    missing_reused_targets = payload["missing_reused_targets"]
    has_blockers = bool(blocking_missing_workflows or missing_reused_targets)
    has_failures = bool(command_failures or tier_readiness_errors)

    summary = {
        "tier": args.tier,
        "run_targets": bool(args.run_targets),
        "json_report": str(json_report_path),
        "markdown_report": str(markdown_report_path),
        "blocking_missing_workflows": blocking_missing_workflows,
        "missing_reused_targets": missing_reused_targets,
        "tier_readiness_errors": tier_readiness_errors,
        "command_failures": command_failures,
        "command_results": [
            {
                "target_id": item.target_id,
                "command": item.command,
                "returncode": item.returncode,
                "output": item.output,
            }
            for item in command_results
        ],
    }

    print(json.dumps(summary, indent=2, sort_keys=True))

    if has_blockers or has_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
