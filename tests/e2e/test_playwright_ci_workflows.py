"""Static contracts for Playwright CI tier ownership and safe failure artifacts."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPO_ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOW_ROOT / name).read_text(encoding="utf-8")


def _assert_failure_only_safe_artifacts(workflow: str) -> None:
    assert "if: failure()" in workflow
    assert "e2e/output/playwright/" in workflow
    assert "e2e/output/playwright-report/" in workflow
    assert "e2e/output/ci-artifacts/" in workflow
    assert 'E2E_CI_ARTIFACT_ROOT: e2e/output/ci-artifacts' in workflow
    assert "trace: retain-on-failure" not in workflow


def test_pr_core_owns_pull_request_chromium_job() -> None:
    workflow = _workflow("e2e-smoke.yml")

    assert "pull_request:" in workflow
    assert "push:" not in workflow
    assert "workflow_dispatch:" not in workflow
    assert "npm run test:e2e:pr" in workflow
    assert "npm run test:e2e:fixture-contracts" in workflow
    assert "npm run test:e2e:journeys" not in workflow
    assert "npm run test:e2e:runtime:local" not in workflow
    assert "playwright install --with-deps chromium" in workflow
    assert "playwright install --with-deps chromium firefox webkit" not in workflow
    _assert_failure_only_safe_artifacts(workflow)


def test_deterministic_journeys_own_main_release_and_manual_runs() -> None:
    workflow = _workflow("e2e-journeys.yml")

    assert "push:" in workflow
    assert "main" in workflow
    assert "release/**" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "npm run test:e2e:journeys" in workflow
    assert "playwright install --with-deps chromium firefox webkit" in workflow
    _assert_failure_only_safe_artifacts(workflow)


def test_runtime_local_owns_nightly_manual_and_explicit_certification() -> None:
    workflow = _workflow("e2e-runtime-local.yml")

    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "release_certification:" in workflow
    assert "runtime-local-canary:" in workflow
    assert "runtime-local-release-certification:" in workflow
    assert workflow.count("npm run test:e2e:runtime:local") == 2
    assert workflow.count("playwright install --with-deps chromium") == 2
    assert "playwright install --with-deps chromium firefox webkit" not in workflow
    assert workflow.count("if: failure()") == 2
    assert workflow.count("e2e/output/ci-artifacts/") >= 2
    assert "pull_request:" not in workflow
    _assert_failure_only_safe_artifacts(workflow)
