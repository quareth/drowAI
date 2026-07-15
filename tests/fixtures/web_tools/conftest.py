from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture
def fixture_base_path() -> Path:
    return Path(__file__).parent


@pytest.fixture
def load_mock_output(fixture_base_path: Path) -> Callable[[str, str], str]:
    """Load mock output for a given category/tool file name."""

    def _loader(category: str, filename: str) -> str:
        path = fixture_base_path / category / filename
        return path.read_text(encoding="utf-8")

    return _loader

