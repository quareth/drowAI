from __future__ import annotations

from pathlib import Path


OUTPUTS_DIR = Path("tests") / "tools" / "fixtures" / "outputs"


def load_output_fixture(tool_id: str) -> str:
    path = OUTPUTS_DIR / f"{tool_id.replace('.', '_')}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Output fixture missing for {tool_id}: {path}")
    return path.read_text(encoding="utf-8")
