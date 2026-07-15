"""Fixtures shared by the LangGraph regression suite."""

from __future__ import annotations

import pytest

from backend.tests.langgraph_regression.harness import RegressionHarness
from backend.tests.langgraph_regression.scenarios import BASELINE_SCENARIOS, SCENARIOS_BY_ID


@pytest.fixture()
def regression_harness() -> RegressionHarness:
    """Provide a deterministic test harness for all regression layers."""
    return RegressionHarness()


@pytest.fixture()
def regression_scenarios():
    """Provide the canonical baseline scenario list."""
    return BASELINE_SCENARIOS


@pytest.fixture()
def regression_scenario_index():
    """Provide a quick scenario lookup by id."""
    return SCENARIOS_BY_ID

