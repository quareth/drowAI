# Web Application Tool Tests

This suite mirrors the execution-model refactor for web application tools. Tests are organized by category and cover three tiers:

- Unit: `agent/tests/tools/web_applications/` (per-tool build/parse/artifacts/run)
- Integration: `tests/integration/` (execution-model compliance, parsing integration)
- E2E: `tests/e2e/` (Kali container smoke tests, skipped when Docker is unavailable)

Running examples:
- `pytest agent/tests/tools/web_applications/`
- `pytest tests/integration/test_web_tools_integration.py`
- `pytest tests/e2e/test_web_tools_kali.py -m e2e`

Fixtures:
- Shared helpers live in `conftest.py` alongside these tests.
- Mock output samples live under `tests/fixtures/web_tools/` grouped by category.

Coverage goal: >90% for tool logic and 100% for `parsing_utils.py`. Extend fixtures/tests alongside new tools to keep parity with the execution model.


