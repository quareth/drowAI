"""Run tiered LangGraph regression gates for CI and local verification."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Dict, List


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(command: List[str]) -> int:
    process = subprocess.run(command, cwd=REPO_ROOT, check=False)
    return process.returncode


def _tier_commands() -> Dict[str, List[List[str]]]:
    python = sys.executable
    return {
        "quick": [
            [
                python,
                "-m",
                "pytest",
                "backend/tests/langgraph_regression",
                "-m",
                "regression_quick",
                "-q",
            ],
        ],
        "main": [
            [
                python,
                "-m",
                "pytest",
                "backend/tests/langgraph_regression",
                "-m",
                "regression_main",
                "-q",
            ],
        ],
        "nightly": [
            [
                python,
                "-m",
                "pytest",
                "backend/tests/langgraph_regression",
                "-m",
                "regression_nightly",
                "-q",
            ],
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=("quick", "main", "nightly"),
        required=True,
        help="Regression gate tier to execute.",
    )
    args = parser.parse_args()

    commands = _tier_commands()[args.tier]
    for command in commands:
        code = _run(command)
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

