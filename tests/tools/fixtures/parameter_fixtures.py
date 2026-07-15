from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


PARAMS_DIR = Path("tests") / "tools" / "fixtures" / "params"


def load_param_fixture(tool_id: str) -> Dict[str, Any]:
    path = PARAMS_DIR / f"{tool_id.replace('.', '_')}.json"
    if not path.exists():
        raise FileNotFoundError(f"Parameter fixture missing for {tool_id}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_param_fixture(tool_id: str, content: Dict[str, Any]) -> Path:
    PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    path = PARAMS_DIR / f"{tool_id.replace('.', '_')}.json"
    path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    return path
