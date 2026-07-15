import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from agent.executor import CommandExecutor
    from agent.models import Action, ActionType, ScopeDocument, ExecutionResult
    from agent.logger import AgentLogger
    from agent.scope_validator import ScopeValidator
except Exception:
    from executor import CommandExecutor
    from models import Action, ActionType, ScopeDocument, ExecutionResult
    from agent.logger import AgentLogger
    from scope_validator import ScopeValidator


import asyncio
import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield mock_client


def _make_config(**overrides):
    attrs = {
        "openai_api_key": "test-key",
        "model_name": "gpt-4",
        "nmap_timeout": 5,
    }
    attrs.update(overrides)
    return type("Cfg", (), attrs)()


def test_scope_validation_blocks_execution(monkeypatch):
    action = Action(type=ActionType.SCAN_PORTS, target="10.0.0.1", parameters={"cmd": ["echo", "scan"]}, reasoning="", expected_outcome="")
    scope = ScopeDocument(targets=["192.168.1.1"], objectives=[], constraints=["No DoS"], methodology=[], time_limit=None, business_hours=None, rate_limits={}, output_format=[])
    logger = AgentLogger(task_id="exec-test")
    executor = CommandExecutor(config=_make_config(), logger=logger)
    validator = ScopeValidator(scope, logger)
    executor.set_scope_validator(validator)

    result = asyncio.run(executor.execute_action(action))
    assert result.exit_code == -1


def test_parse_nmap_output():
    logger = AgentLogger(task_id="parse-test")
    executor = CommandExecutor(config=_make_config(), logger=logger)

    sample_output = (
        "Starting Nmap 7.93\n"
        "Host is up (0.10s latency).\n"
        "PORT   STATE SERVICE\n"
        "22/tcp open ssh\n"
        "80/tcp open http\n"
    )

    result = ExecutionResult(success=True, stdout=sample_output, stderr="", exit_code=0)
    action = Action(type=ActionType.SCAN_PORTS, target="1.2.3.4", parameters={}, reasoning="", expected_outcome="")

    findings = executor.parse_results(result, action)

    titles = [f.title for f in findings]
    assert "Host is up" in titles
    assert "Open port 22/tcp" in titles
    assert len(findings) == 3


def test_nmap_debug_logging(monkeypatch):
    """Verify nmap command and output are logged as debug react steps."""

    logs = []

    class StubLogger:
        def log_operation(self, level, message, metadata=None):
            logs.append(("log_operation", message))

        def debug(self, message, details=None):
            logs.append(("debug", message))

        def log_reasoning_step(self, step_type, content, metadata=None):
            logs.append((step_type, content))

        def log_command(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    logger = StubLogger()
    executor = CommandExecutor(config=_make_config(nmap_timeout=5), logger=logger)

    class DummyProc:
        def __init__(self):
            self.returncode = 0

        async def communicate(self):
            return b"output", b""

    async def fake_subproc(cmd, stdout=None, stderr=None):
        return DummyProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_subproc)

    async def dummy_store(*args, **kwargs):
        return None

    monkeypatch.setattr(executor, "_store_command_output", dummy_store)

    result = asyncio.run(executor._execute_nmap_scan("1.2.3.4"))

    assert result.success
    assert any("[DEBUG_NMAP_COMMAND]" in msg for _, msg in logs)
    assert any("[DEBUG_NMAP_OUTPUT]" in msg for _, msg in logs)
