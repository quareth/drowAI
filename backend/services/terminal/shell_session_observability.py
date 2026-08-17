"""Operational observation for provider-backed shell sessions.

This module owns shell-session logs, identifier fingerprinting, counters, and
active-session gauges. It owns no logical state, PTY I/O, chat projection, or
shell content.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from backend.core.logging import safe_identifier_fingerprint
from backend.services.metrics.utils import safe_gauge, safe_inc
from runtime_shared.shell_session_contracts import (
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
)

logger = logging.getLogger("backend.services.terminal.shell_session_service")


class ShellSessionOperationalObserver:
    """Emit the stable operational log and metric contract for shell sessions."""

    def session_opened(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
        active_placements: Iterable[str],
    ) -> None:
        """Observe a committed logical session registration."""

        placement = self.placement(identity.runtime_placement_mode)
        logger.info(
            (
                "shell_session event=session_opened tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s"
            ),
            identity.tenant_id,
            identity.task_id,
            safe_identifier_fingerprint(identity.execution_owner_id),
            safe_identifier_fingerprint(public_session_id),
            placement,
        )
        safe_inc("shell_session_starts")
        self.active_session_gauges(
            changed_placements=(placement,),
            active_placements=active_placements,
        )

    def process_completed(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
        process_status: ShellProcessStatus,
    ) -> None:
        """Observe a terminal process outcome."""

        placement = self.placement(identity.runtime_placement_mode)
        process_status_value = process_status.value
        logger.info(
            (
                "shell_session event=process_completed tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s process_status=%s"
            ),
            identity.tenant_id,
            identity.task_id,
            safe_identifier_fingerprint(identity.execution_owner_id),
            safe_identifier_fingerprint(public_session_id),
            placement,
            process_status_value,
        )
        safe_inc(f"shell_session_terminal_outcomes.{process_status_value}")

    def session_closed(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
        close_reason: str,
    ) -> None:
        """Observe a logical session close before chat lifecycle projection."""

        placement = self.placement(identity.runtime_placement_mode)
        logger.info(
            (
                "shell_session event=session_closed tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s close_reason=%s"
            ),
            identity.tenant_id,
            identity.task_id,
            safe_identifier_fingerprint(identity.execution_owner_id),
            safe_identifier_fingerprint(public_session_id),
            placement,
            self.stable_segment(close_reason),
        )

    def operation_failed(
        self,
        *,
        identity: ShellSessionIdentity,
        error_code: ShellSessionErrorCode,
        public_session_id: str | None = None,
    ) -> None:
        """Observe a public shell operation failure without retaining content."""

        placement = self.placement(identity.runtime_placement_mode)
        error_code_value = error_code.value
        logger.info(
            (
                "shell_session event=operation_failed tenant_id=%s task_id=%s "
                "owner_fp=%s session_fp=%s placement=%s error_code=%s"
            ),
            identity.tenant_id,
            identity.task_id,
            safe_identifier_fingerprint(identity.execution_owner_id),
            safe_identifier_fingerprint(public_session_id or ""),
            placement,
            error_code_value,
        )
        safe_inc(f"shell_session_operation_failures.{error_code_value}")

    def active_session_gauges(
        self,
        *,
        changed_placements: Iterable[str],
        active_placements: Iterable[str],
    ) -> None:
        """Project active counts for changed and currently active placements."""

        normalized_active = [self.placement(value) for value in active_placements]
        placements = {
            self.placement(value) for value in changed_placements
        }
        placements.update(normalized_active)
        for placement in placements:
            safe_gauge(
                f"shell_session_active_sessions.{placement}",
                normalized_active.count(placement),
            )

    @classmethod
    def placement(cls, value: str | None) -> str:
        """Normalize runtime placement to the stable metric/log vocabulary."""

        normalized = cls.stable_segment(value or "unknown")
        if normalized in {"local", "runner", "managed_runner"}:
            return normalized
        return "unknown"

    @staticmethod
    def stable_segment(value: str) -> str:
        """Normalize a dynamic value into a stable safe metric/log segment."""

        normalized = "".join(
            char if char.isalnum() or char == "_" else "_"
            for char in str(value).strip().lower()
        ).strip("_")
        return normalized or "unknown"
