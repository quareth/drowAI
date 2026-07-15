#!/usr/bin/env python3
"""Regression test runner for unified emitter migration validation.

Orchestrates test execution across all test directories, collects metrics,
and generates a validation report. Follows the pattern established in
run_phase1_tests.py."""

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass
class PhaseResult:
    """Result of running a single test phase."""
    name: str
    path: str
    passed: int
    failed: int
    skipped: int
    duration_seconds: float
    error: Optional[str] = None
    slowest_tests: List[Tuple[str, float]] = field(default_factory=list)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_by_directory(
    project_root: Path,
    stop_on_first_failure: bool = False,
    durations: int = 10,
) -> List[PhaseResult]:
    """Run tests grouped by directory. Each suite runs exactly once (no duplicate paths)."""
    phases = [
        ("Validation (emission)", str(project_root / "backend" / "tests" / "emission" / "test_no_legacy_helpers.py"), []),
        ("Backend", str(project_root / "backend" / "tests"), ["-k", "not emission"]),
        ("Agent graph", str(project_root / "agent" / "graph" / "tests"), []),
        ("Integration (tests/)", str(project_root / "tests"), []),
    ]
    results: List[PhaseResult] = []
    for name, path, extra_args in phases:
        if not Path(path).exists():
            results.append(PhaseResult(
                name=name,
                path=path,
                passed=0,
                failed=0,
                skipped=0,
                duration_seconds=0.0,
                error=f"Path does not exist: {path}",
            ))
            continue
        cmd = [
            sys.executable, "-m", "pytest", path,
            "-v", "--tb=short", f"--durations={durations}",
            *extra_args,
        ]
        if stop_on_first_failure:
            cmd.append("-x")
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            results.append(PhaseResult(
                name=name,
                path=path,
                passed=0,
                failed=0,
                skipped=0,
                duration_seconds=time.perf_counter() - start,
                error="Timeout (600s)",
            ))
            continue
        except Exception as e:
            results.append(PhaseResult(
                name=name,
                path=path,
                passed=0,
                failed=0,
                skipped=0,
                duration_seconds=time.perf_counter() - start,
                error=str(e),
            ))
            continue
        duration = time.perf_counter() - start
        # Parse pytest output for pass/fail/skip counts (last line often has summary)
        out = proc.stdout + proc.stderr
        passed = failed = skipped = 0
        for line in out.splitlines():
            if " passed" in line or " passed," in line:
                parts = line.replace(",", " ").split()
                for i, p in enumerate(parts):
                    if p == "passed" and i > 0:
                        try:
                            passed = int(parts[i - 1])
                        except ValueError:
                            pass
                        break
            if " failed" in line or " failed," in line:
                parts = line.replace(",", " ").split()
                for i, p in enumerate(parts):
                    if p == "failed" and i > 0:
                        try:
                            failed = int(parts[i - 1])
                        except ValueError:
                            pass
                        break
            if " skipped" in line or " skipped," in line:
                parts = line.replace(",", " ").split()
                for i, p in enumerate(parts):
                    if p == "skipped" and i > 0:
                        try:
                            skipped = int(parts[i - 1])
                        except ValueError:
                            pass
                        break
        # Fallback: use return code
        if passed == 0 and failed == 0 and proc.returncode != 0:
            failed = 1
        if passed == 0 and failed == 0 and proc.returncode == 0:
            # Try to count from "X passed" style
            for word in out.replace(",", " ").split():
                if word == "passed":
                    idx = out.replace(",", " ").split().index("passed")
                    if idx > 0:
                        try:
                            passed = int(out.replace(",", " ").split()[idx - 1])
                        except (ValueError, IndexError):
                            pass
                    break
        results.append(PhaseResult(
            name=name,
            path=path,
            passed=passed,
            failed=failed,
            skipped=skipped,
            duration_seconds=duration,
            error=None if proc.returncode == 0 else "pytest exited with non-zero code",
            slowest_tests=[],  # Could parse --durations output if needed
        ))
    return results


def run_all_tests(
    project_root: Path,
    stop_on_first_failure: bool = False,
    durations: int = 10,
) -> PhaseResult:
    """Execute full regression suite (all directories in one pytest run). backend/tests already includes langgraph_chat."""
    paths = [
        str(project_root / "backend" / "tests"),
        str(project_root / "agent" / "graph" / "tests"),
        str(project_root / "tests"),
    ]
    cmd = [
        sys.executable, "-m", "pytest",
        *paths,
        "-v", "--tb=short", f"--durations={durations}",
    ]
    if stop_on_first_failure:
        cmd.append("-x")
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return PhaseResult(
            name="Full suite",
            path=", ".join(paths),
            passed=0,
            failed=0,
            skipped=0,
            duration_seconds=time.perf_counter() - start,
            error="Timeout (900s)",
        )
    except Exception as e:
        return PhaseResult(
            name="Full suite",
            path=", ".join(paths),
            passed=0,
            failed=0,
            skipped=0,
            duration_seconds=time.perf_counter() - start,
            error=str(e),
        )
    duration = time.perf_counter() - start
    out = proc.stdout + proc.stderr
    passed = failed = skipped = 0
    for line in out.splitlines():
        if " passed" in line:
            parts = line.replace(",", " ").split()
            for i, p in enumerate(parts):
                if p == "passed" and i > 0:
                    try:
                        passed = int(parts[i - 1])
                    except ValueError:
                        pass
                    break
        if " failed" in line:
            parts = line.replace(",", " ").split()
            for i, p in enumerate(parts):
                if p == "failed" and i > 0:
                    try:
                        failed = int(parts[i - 1])
                    except ValueError:
                        pass
                    break
        if " skipped" in line:
            parts = line.replace(",", " ").split()
            for i, p in enumerate(parts):
                if p == "skipped" and i > 0:
                    try:
                        skipped = int(parts[i - 1])
                    except ValueError:
                        pass
                    break
    if passed == 0 and failed == 0 and proc.returncode != 0:
        failed = 1
    return PhaseResult(
        name="Full suite",
        path=", ".join(paths),
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration,
        error=None if proc.returncode == 0 else "pytest exited with non-zero code",
        slowest_tests=[],
    )


def generate_report(
    phase_results: List[PhaseResult],
    full_suite_result: Optional[PhaseResult] = None,
    output_path: Optional[Path] = None,
) -> str:
    """Create markdown summary of test results."""
    lines = [
        "# Regression Suite Report",
        "",
        "## Summary",
        "",
    ]
    total_passed = sum(r.passed for r in phase_results)
    total_failed = sum(r.failed for r in phase_results)
    total_skipped = sum(r.skipped for r in phase_results)
    total_duration = sum(r.duration_seconds for r in phase_results)
    lines.append(f"- **Total tests run:** {total_passed + total_failed + total_skipped}")
    lines.append(f"- **Passed:** {total_passed}")
    lines.append(f"- **Failed:** {total_failed}")
    lines.append(f"- **Skipped:** {total_skipped}")
    lines.append(f"- **Duration:** {total_duration:.2f}s")
    lines.append("")
    lines.append("## Results by Phase")
    lines.append("")
    for r in phase_results:
        status = "✅" if r.failed == 0 and r.error is None else "❌"
        lines.append(f"### {r.name} {status}")
        lines.append(f"- Path: `{r.path}`")
        lines.append(f"- Passed: {r.passed}, Failed: {r.failed}, Skipped: {r.skipped}")
        lines.append(f"- Duration: {r.duration_seconds:.2f}s")
        if r.error:
            lines.append(f"- Error: {r.error}")
        lines.append("")
    if full_suite_result:
        lines.append("## Full Suite (combined run)")
        lines.append("")
        status = "✅" if full_suite_result.failed == 0 and full_suite_result.error is None else "❌"
        lines.append(f"- {status} Passed: {full_suite_result.passed}, Failed: {full_suite_result.failed}, Skipped: {full_suite_result.skipped}")
        lines.append(f"- Duration: {full_suite_result.duration_seconds:.2f}s")
        if full_suite_result.error:
            lines.append(f"- Error: {full_suite_result.error}")
        lines.append("")
    report = "\n".join(lines)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    return report


def compare_with_baseline(
    phase_results: List[PhaseResult],
    baseline_path: Optional[Path] = None,
) -> List[str]:
    """Detect regressions compared to a baseline. Returns list of regression messages."""
    regressions: List[str] = []
    if not baseline_path or not baseline_path.exists():
        return regressions
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception:
        return regressions
    baseline = data.get("phases", {})
    for r in phase_results:
        name = r.name
        if name in baseline:
            b = baseline[name]
            if r.failed > b.get("failed", 0):
                regressions.append(
                    f"{name}: failures increased from {b.get('failed', 0)} to {r.failed}"
                )
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Phase 5 regression test suite"
    )
    parser.add_argument(
        "--by-directory",
        action="store_true",
        help="Run tests grouped by directory (default: run full suite only)",
    )
    parser.add_argument(
        "-x",
        "--exitfirst",
        action="store_true",
        dest="exitfirst",
        help="Stop on first failure",
    )
    parser.add_argument(
        "--durations",
        type=int,
        default=10,
        help="Show N slowest tests (default: 10)",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=None,
        metavar="PATH",
        help="Write markdown report to PATH",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default=None,
        metavar="PATH",
        help="JSON baseline file for regression comparison",
    )
    args = parser.parse_args()
    project_root = _project_root()

    if args.by_directory:
        phase_results = run_by_directory(
            project_root,
            stop_on_first_failure=args.exitfirst,
            durations=args.durations,
        )
        print("=" * 80)
        print("PHASE 5 REGRESSION SUITE - BY DIRECTORY")
        print("=" * 80)
        for r in phase_results:
            status = "PASS" if r.failed == 0 and r.error is None else "FAIL"
            print(f"  [{status}] {r.name}: {r.passed} passed, {r.failed} failed, {r.skipped} skipped ({r.duration_seconds:.2f}s)")
            if r.error:
                print(f"         Error: {r.error}")
        total_failed = sum(r.failed for r in phase_results)
        has_error = any(r.error for r in phase_results)
        if args.report:
            report_path = Path(args.report)
            generate_report(phase_results, output_path=report_path)
            print(f"\nReport written to {report_path}")
        if args.baseline:
            regressions = compare_with_baseline(phase_results, Path(args.baseline))
            for msg in regressions:
                print(f"REGRESSION: {msg}")
        print()
        success = total_failed == 0 and not has_error
    else:
        result = run_all_tests(
            project_root,
            stop_on_first_failure=args.exitfirst,
            durations=args.durations,
        )
        print("=" * 80)
        print("PHASE 5 REGRESSION SUITE - FULL")
        print("=" * 80)
        status = "PASS" if result.failed == 0 and result.error is None else "FAIL"
        print(f"  [{status}] {result.passed} passed, {result.failed} failed, {result.skipped} skipped ({result.duration_seconds:.2f}s)")
        if result.error:
            print(f"  Error: {result.error}")
        if args.report:
            generate_report([result], full_suite_result=result, output_path=Path(args.report))
        success = result.failed == 0 and result.error is None

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
