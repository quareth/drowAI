"""Contract tests for the Playwright PR and deterministic journey tier routing."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_LOCAL_SELECTOR = "@runtime-local"


def test_package_scripts_expose_pr_and_multi_browser_journey_tiers() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]

    assert scripts["test:e2e:fixture-contracts"] == (
        "node --import tsx --test e2e/fixtures/*.test.ts"
    )
    quick_fixture_command = scripts["test:e2e:fixture-contracts:quick"]
    assert "node --import tsx --test" in quick_fixture_command
    assert ".integration.test.ts" not in quick_fixture_command

    pr_command = scripts["test:e2e:pr"]
    assert "--grep @pr-core" in pr_command
    assert "--project=chromium" in pr_command
    assert "@journey" not in pr_command
    assert RUNTIME_LOCAL_SELECTOR not in pr_command
    assert "test:e2e:smoke" not in scripts
    assert "test:e2e:quick" not in scripts
    assert "test:e2e:full" not in scripts

    journey_command = scripts["test:e2e:journeys"]
    assert "--grep @journey" in journey_command
    assert "--project" not in journey_command
    assert "@pr-core" not in journey_command
    assert RUNTIME_LOCAL_SELECTOR not in journey_command
    chromium_journey_command = scripts["test:e2e:journeys:chromium"]
    assert chromium_journey_command.startswith(journey_command)
    assert chromium_journey_command.endswith("--project=chromium")
    assert scripts["test:e2e:journeys:all"] == journey_command
    runtime_command = scripts["test:e2e:runtime:local"]
    assert "--grep @runtime-local" in runtime_command
    assert "--project=chromium" in runtime_command
    assert "E2E_RUNTIME_LOCAL_MODE=true" in runtime_command
    assert "E2E_DETERMINISTIC_MODE=false" in runtime_command


def test_runtime_local_spec_is_real_ui_canary() -> None:
    spec = (REPO_ROOT / "e2e/tests/runtime-local-canary.spec.ts").read_text(
        encoding="utf-8"
    )

    assert RUNTIME_LOCAL_SELECTOR in spec
    assert "assertRuntimeLocalPrerequisites" in spec
    assert "createTaskThroughUiForEngagement" in spec
    assert "runTaskActionThroughUi" in spec
    assert "runtime-canary.txt" in spec
    assert "page.route(" not in spec
    assert "test.skip" not in spec


def test_playwright_config_has_three_browsers_and_no_retry_masking() -> None:
    config = (REPO_ROOT / "e2e/playwright.config.ts").read_text(encoding="utf-8")

    assert f'runtimeLocal: "{RUNTIME_LOCAL_SELECTOR}"' in config
    assert "retries: 0" in config
    assert "workers: 1" in config
    assert 'trace: "off"' in config
    assert 'trace: "retain-on-failure"' not in config
    assert config.count('name: "chromium"') == 1
    assert config.count('name: "firefox"') == 1
    assert config.count('name: "webkit"') == 1


def test_pr_core_and_persisted_knowledge_specs_have_disjoint_tier_tags() -> None:
    core_spec = (REPO_ROOT / "e2e/tests/pr-core.spec.ts").read_text(
        encoding="utf-8"
    )
    persisted_knowledge_spec = (
        REPO_ROOT / "e2e/tests/engagement-knowledge-workspace.spec.ts"
    ).read_text(encoding="utf-8")

    assert "@pr-core" in core_spec
    assert "@journey" in core_spec
    assert "@pr-core" not in persisted_knowledge_spec
    assert "@journey" in persisted_knowledge_spec
    assert "page.route(" not in persisted_knowledge_spec
    assert "route.fulfill(" not in persisted_knowledge_spec
