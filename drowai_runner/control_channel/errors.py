"""Control-channel error types.

Exception definitions only; no logic, no I/O, and no imports from sibling
control_channel modules.
"""

from __future__ import annotations


class RunnerCloudClientError(RuntimeError):
    """Raised when managed runner mode cannot continue safely."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
