"""Tests for DrowAI mechanical report safety and classification contracts."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / ".codex/skills/drowai-tool-mechanical-validation"
SCRIPT_PATH = SKILL_ROOT / "scripts/validate_mechanical_report.py"
FIXTURE_ROOT = REPO_ROOT / "tests/codex_workflows/fixtures"
VALID_REPORT_PATH = FIXTURE_ROOT / "valid_mechanical_report.json"
INVALID_SECRET_PATH = FIXTURE_ROOT / "invalid_mechanical_report_with_secret.json"
KALITOOL_SCRIPT = (
    REPO_ROOT / ".codex/skills/kalitool/scripts/run_real_kali_tool_schema_test.py"
)


def _load_validator():
    spec = importlib.util.spec_from_file_location("mechanical_report_validator", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_report() -> dict[str, object]:
    return json.loads(VALID_REPORT_PATH.read_text(encoding="utf-8"))


def test_valid_mechanical_report_passes() -> None:
    validator = _load_validator()
    report = _valid_report()

    assert validator.validate_report(
        report,
        raw_text=json.dumps(report),
    ) == []


def test_missing_nvidia_model_and_compression_counts_fail() -> None:
    validator = _load_validator()
    report = _valid_report()
    del report["model"]["preset_id"]  # type: ignore[index]
    del report["compression"]["shown"]  # type: ignore[index]

    errors = validator.validate_report(report, raw_text=json.dumps(report))

    assert "model.preset_id" in errors
    assert "compression.counts" in errors


def test_missing_schema_cannot_be_inconclusive() -> None:
    validator = _load_validator()
    report = _valid_report()
    report["final_status"] = "INCONCLUSIVE"
    report["gui"]["selection_attempts"] = 2  # type: ignore[index]
    report["gui"]["selection_classification"] = "INCONCLUSIVE_MODEL_SELECTION"  # type: ignore[index]
    report["schema_runs"]["minimal"]["status"] = "missing"  # type: ignore[index]

    errors = validator.validate_report(report, raw_text=json.dumps(report))

    assert "schema_runs.minimal.status" in errors


def test_public_domain_and_quality_scoring_are_rejected() -> None:
    validator = _load_validator()
    report = _valid_report()
    report["safe_target"]["value"] = "example.com"  # type: ignore[index]
    report["prompt-quality"] = "excellent"

    errors = validator.validate_report(report, raw_text=json.dumps(report))

    assert "safe_target.value" in errors
    assert "quality_scoring_forbidden" in errors


def test_secret_fixture_fails_without_echoing_secret() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(INVALID_SECRET_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "secret_material" in result.stdout
    assert "validation-token-must-never-be-recorded" not in result.stdout


def test_kalitool_uses_reserved_domain_not_public_example() -> None:
    source = KALITOOL_SCRIPT.read_text(encoding="utf-8")

    assert 'SAFE_DOMAIN = "drowai.test"' in source
    assert 'SAFE_DOMAIN = "example.com"' not in source


def test_skill_is_codex_canonical_and_has_no_todos() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in SKILL_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".md", ".py", ".toml", ".yaml"}
    )

    assert ".cursor" not in content
    assert "[TODO" not in content
    assert "prompt quality" in content.lower()
