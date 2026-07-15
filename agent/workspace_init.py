"""Initialize the container workspace structure."""

import os
from pathlib import Path

from runtime_shared.file_comm_contracts import (
    LOCKS_DIRECTORY_NAME,
    STANDARD_LOCK_FILES,
    STANDARD_RUNTIME_FILES,
    STANDARD_RUNTIME_SUBDIRECTORIES,
)


def init_workspace(path: str = "/workspace") -> None:
    workspace = Path(path)
    workspace.mkdir(parents=True, exist_ok=True)
    for sub in STANDARD_RUNTIME_SUBDIRECTORIES:
        (workspace / sub).mkdir(exist_ok=True, mode=0o755)

    files = [workspace / name for name in STANDARD_RUNTIME_FILES]
    files.extend(workspace / LOCKS_DIRECTORY_NAME / name for name in STANDARD_LOCK_FILES)
    for file in files:
        file.touch(exist_ok=True)
        os.chmod(file, 0o644)


if __name__ == "__main__":
    init_workspace(os.environ.get("WORKSPACE", "/workspace"))
