"""Golden-file helpers for characterization tests.

Set `DROWAI_UPDATE_GOLDENS=1` to (re)generate expected outputs locally.
"""

from __future__ import annotations

import os
from pathlib import Path


GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def assert_golden(name: str, actual: str) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path = GOLDEN_DIR / name

    actual_norm = _normalize(actual)

    if os.getenv("DROWAI_UPDATE_GOLDENS") == "1":
        golden_path.write_text(actual_norm, encoding="utf-8")
        return

    expected = golden_path.read_text(encoding="utf-8")
    expected_norm = _normalize(expected)
    assert actual_norm == expected_norm

