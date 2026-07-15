import os
import runpy


def test_context_quick_start_example(tmp_path, monkeypatch):
    # Set workspace path to tmp to avoid side effects
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))
    example_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "examples", "context_quick_start.py")
    result = runpy.run_path(example_path)
    assert "ctx" in result or "manager" in result
