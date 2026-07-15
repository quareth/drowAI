"""Tests for the tracked test-inventory generator."""

from __future__ import annotations

from pathlib import Path

from scripts import generate_test_inventory as inventory


def _write_release_gate(root: Path) -> None:
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "run_release_gate.py").write_text(
        """
def _commands():
    quick_backend_paths = ("backend/tests/test_auth.py",)
    main_backend_paths = ("tests/runner/test_status.py",)
    frontend_contract_paths = ("client/src/lib/auth.test.ts",)
    fixture_contract_paths = ("e2e/fixtures/security.test.ts",)
""".strip(),
        encoding="utf-8",
    )


def test_is_test_path_ignores_helpers_and_accepts_supported_frameworks() -> None:
    assert inventory.is_test_path("backend/tests/test_auth.py")
    assert inventory.is_test_path("client/src/auth.test.ts")
    assert inventory.is_test_path("e2e/tests/smoke.spec.ts")
    assert not inventory.is_test_path("backend/tests/conftest.py")
    assert not inventory.is_test_path("client/src/testing/helpers.ts")


def test_framework_distinguishes_node_test_from_vitest() -> None:
    assert (
        inventory._framework(
            "e2e/fixtures/security.test.ts", 'import test from "node:test";'
        )
        == "node:test"
    )
    assert (
        inventory._framework(
            "client/src/auth.test.ts", 'import { test } from "vitest";'
        )
        == "vitest"
    )


def test_build_inventory_derives_gate_membership_and_untriaged_status(tmp_path: Path) -> None:
    _write_release_gate(tmp_path)
    workflow_root = tmp_path / ".github/workflows"
    workflow_root.mkdir(parents=True)
    (workflow_root / "e2e-smoke.yml").write_text(
        "run: npm run test:e2e:fixture-contracts\nrun: npm run test:e2e:pr\n",
        encoding="utf-8",
    )
    (workflow_root / "e2e-journeys.yml").write_text(
        "run: npm run test:e2e:journeys\n", encoding="utf-8"
    )
    (workflow_root / "e2e-runtime-local.yml").write_text(
        "run: npm run test:e2e:runtime:local\n", encoding="utf-8"
    )
    files = {
        "backend/tests/test_auth.py": "def test_auth(): pass\n",
        "tests/runner/test_status.py": "def test_status(): pass\n",
        "agent/tests/test_tool.py": "def test_tool(): pass\n",
        "backend/tests/langgraph_regression/test_flow.py": (
            "import pytest\n@pytest.mark.regression_quick\ndef test_flow(): pass\n"
        ),
        "e2e/tests/pr-core.spec.ts": (
            "test('smoke', { tag: ['@pr-core', '@journey'] }, async () => {});\n"
        ),
        "e2e/tests/owner-core-journey.spec.ts": (
            "test('owner', { tag: '@journey' }, async () => {});\n"
        ),
        "e2e/tests/both-tiers.spec.ts": (
            "test('critical', { tag: ['@pr-core', '@journey'] }, async () => {});\n"
        ),
        "e2e/tests/runtime.spec.ts": (
            "test('runtime', { tag: '@runtime-local' }, async () => {});\n"
        ),
        "e2e/fixtures/security.test.ts": 'import test from "node:test";\n',
        "e2e/fixtures/live.integration.test.ts": 'import test from "node:test";\n',
        "e2e/probes/authenticated-failure-artifact.spec.ts": (
            "test('artifact probe', async () => {});\n"
        ),
    }
    for path, content in files.items():
        file_path = tmp_path / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    entries = inventory.build_inventory(tmp_path, paths=files)
    by_path = {entry.path: entry for entry in entries}

    assert by_path["backend/tests/test_auth.py"].trust_status == "trusted-ci-selection"
    assert by_path["tests/runner/test_status.py"].trust_status == "curated-manual"
    assert by_path["agent/tests/test_tool.py"].trust_status == "untriaged"
    assert "release-quick-ci" in by_path[
        "backend/tests/langgraph_regression/test_flow.py"
    ].gate_memberships
    assert "marked tests" in by_path[
        "backend/tests/langgraph_regression/test_flow.py"
    ].notes
    assert "e2e-pr-ci-configured" in by_path[
        "e2e/tests/pr-core.spec.ts"
    ].gate_memberships
    assert by_path["e2e/tests/owner-core-journey.spec.ts"].gate_memberships == (
        "e2e-journeys-main-release-ci",
    )
    assert by_path["e2e/tests/both-tiers.spec.ts"].gate_memberships == (
        "e2e-journeys-main-release-ci",
        "e2e-pr-ci-configured",
    )
    assert by_path["e2e/tests/runtime.spec.ts"].gate_memberships == (
        "e2e-runtime-nightly-release-manual",
    )
    assert by_path["e2e/fixtures/security.test.ts"].framework == "node:test"
    assert by_path["e2e/fixtures/security.test.ts"].gate_memberships == (
        "e2e-fixture-contracts-pr-ci",
        "release-main-manual",
        "release-quick-ci",
    )
    assert by_path["e2e/fixtures/security.test.ts"].trust_status == "trusted-ci-selection"
    assert by_path["e2e/fixtures/live.integration.test.ts"].gate_memberships == (
        "e2e-fixture-contracts-pr-ci",
    )
    assert by_path["e2e/fixtures/live.integration.test.ts"].trust_status == "candidate-e2e"
    assert by_path[
        "e2e/probes/authenticated-failure-artifact.spec.ts"
    ].gate_memberships == ("e2e-fixture-contracts-pr-ci",)
    assert by_path[
        "e2e/probes/authenticated-failure-artifact.spec.ts"
    ].layer == "integration"


def test_renderers_are_deterministic_and_preserve_timing_evidence(tmp_path: Path) -> None:
    _write_release_gate(tmp_path)
    path = "backend/tests/test_auth.py"
    file_path = tmp_path / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("def test_auth(): pass\n", encoding="utf-8")

    entries = inventory.build_inventory(
        tmp_path,
        paths=[path],
        timings={path: 1.25},
        overrides={path: {"trust_status": "useful-slow", "notes": "Measured locally."}},
    )
    csv_report = inventory.render_csv(entries)
    summary = inventory.render_summary(entries)

    assert "1.250" in csv_report
    assert "useful-slow" in csv_report
    assert "Measured locally." in csv_report
    assert "Repository test files: **1**" in summary
    assert "Files with measured duration evidence: **1**" in summary
