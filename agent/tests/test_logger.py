import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.logger import AgentLogger


def test_logger_writes_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    log_path = tmp_path / "log.txt"
    error_path = tmp_path / "error.log"
    log_path.touch()
    error_path.touch()

    logger = AgentLogger('task-1')
    logger.conversation('starting test')
    content = log_path.read_text(encoding="utf-8")
    assert 'starting test' in content

    logger.error('boom')
    with error_path.open(encoding="utf-8") as f:
        line = f.readlines()[-1]
    json_part = line.split(' - ', 3)[-1]
    entry = json.loads(json_part)
    assert entry['level'] == 'ERROR'
    assert entry['message'] == 'boom'
