"""State-machine tests for the logical shell-session registry."""

from __future__ import annotations

import asyncio

import pytest

from backend.services.terminal.registry import (
    ShellSessionRecord,
    ShellSessionStateRegistry,
)
from backend.services.terminal.shell_session_observability import (
    ShellSessionOperationalObserver,
)
from runtime_shared.shell_capabilities import ShellCapability
from runtime_shared.shell_session_contracts import (
    ShellSessionErrorCode,
    ShellSessionIdentity,
)


def _identity(
    *,
    tenant_id: int = 1,
    task_id: int = 2,
    owner: str = "main:turn-1",
) -> ShellSessionIdentity:
    return ShellSessionIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        execution_owner_id=owner,
        runtime_placement_mode="runner",
        workspace_id=f"task-{task_id}",
        workspace_path="/workspace",
        runner_id="runner-1",
        execution_site_id=None,
    )


def _registry(
    *,
    owner_limit: int = 2,
    task_limit: int = 3,
    idle_timeout: float = 10.0,
) -> ShellSessionStateRegistry:
    return ShellSessionStateRegistry(
        max_active_per_owner=owner_limit,
        max_active_per_task=task_limit,
        idle_timeout_sec=idle_timeout,
        observer=ShellSessionOperationalObserver(),
    )


def _record(
    session_id: str,
    *,
    identity: ShellSessionIdentity | None = None,
    last_activity_at: float = 5.0,
    deadline_at: float = 20.0,
) -> ShellSessionRecord:
    return ShellSessionRecord(
        public_session_id=session_id,
        terminal_session_id=f"terminal-{session_id}",
        identity=identity or _identity(),
        originating_capability=ShellCapability.ASSESSMENT,
        origin=None,
        last_activity_at=last_activity_at,
        deadline_at=deadline_at,
        interactive=True,
        pending_utf8_bytes=b"tail",
        initial_quiet_boundary_emitted=True,
    )


@pytest.mark.asyncio
async def test_concurrent_reservations_enforce_capacity_and_release_idempotently() -> None:
    registry = _registry(owner_limit=1, task_limit=1)
    identity = _identity()

    reservations = await asyncio.gather(
        *(registry.reserve_start(identity) for _ in range(8))
    )
    accepted = [reservation for reservation in reservations if reservation is not None]
    assert len(accepted) == 1

    await registry.release_start(accepted[0])
    await registry.release_start(accepted[0])
    assert await registry.reserve_start(identity) is not None


@pytest.mark.asyncio
async def test_registration_claim_release_and_full_identity_matching() -> None:
    registry = _registry()
    identity = _identity()
    reservation = await registry.reserve_start(identity)
    assert reservation is not None
    record = _record("shs_one", identity=identity)
    assert not hasattr(record, "socket")
    assert not hasattr(record, "provider_session_id")
    assert not hasattr(record, "command")
    assert not hasattr(record, "transcript")
    assert not hasattr(record, "raw_output")
    await registry.register(record, reservation=reservation)

    assert await registry.get_capability(
        identity=identity,
        public_session_id=record.public_session_id,
    ) is ShellCapability.ASSESSMENT
    mismatch = _identity(owner="main:other")
    assert await registry.get_capability(
        identity=mismatch,
        public_session_id=record.public_session_id,
    ) is None

    claimed, retired, error, close_reason = await registry.claim(
        identity=identity,
        public_session_id=record.public_session_id,
        now=6.0,
    )
    assert claimed is record
    assert retired is None
    assert error is ShellSessionErrorCode.SESSION_UNAVAILABLE
    assert close_reason is None
    assert record.last_activity_at == 6.0
    assert record.pending_utf8_bytes == b"tail"
    assert record.initial_quiet_boundary_emitted is True

    busy, _, busy_error, _ = await registry.claim(
        identity=identity,
        public_session_id=record.public_session_id,
        now=7.0,
    )
    assert busy is None
    assert busy_error is ShellSessionErrorCode.SESSION_BUSY

    await registry.release(record)
    claimed_again, _, _, _ = await registry.claim(
        identity=identity,
        public_session_id=record.public_session_id,
        now=8.0,
    )
    assert claimed_again is record


@pytest.mark.asyncio
async def test_deadline_precedes_idle_expiry_and_expected_record_guards_removal() -> None:
    registry = _registry(idle_timeout=1.0)
    identity = _identity()
    reservation = await registry.reserve_start(identity)
    assert reservation is not None
    record = _record(
        "shs_expired",
        identity=identity,
        last_activity_at=0.0,
        deadline_at=1.0,
    )
    await registry.register(record, reservation=reservation)

    claimed, retired, error, close_reason = await registry.claim(
        identity=identity,
        public_session_id=record.public_session_id,
        now=2.0,
    )
    assert claimed is None
    assert retired is record
    assert error is ShellSessionErrorCode.COMMAND_TIMED_OUT
    assert close_reason == "deadline_expired"

    replacement_reservation = await registry.reserve_start(identity)
    assert replacement_reservation is not None
    replacement = _record("shs_expired", identity=identity)
    await registry.register(replacement, reservation=replacement_reservation)
    assert await registry.remove(
        replacement.public_session_id,
        expected_record=record,
    ) is None
    assert await registry.get_capability(
        identity=identity,
        public_session_id=replacement.public_session_id,
    ) is ShellCapability.ASSESSMENT


@pytest.mark.asyncio
async def test_claimed_record_ignores_idle_expiry_until_operation_releases() -> None:
    """Active coordination prevents idle retirement without extending the deadline."""
    registry = _registry(idle_timeout=1.0)
    identity = _identity()
    reservation = await registry.reserve_start(identity)
    assert reservation is not None
    record = _record(
        "shs_claimed_idle",
        identity=identity,
        last_activity_at=0.0,
        deadline_at=10.0,
    )
    await registry.register(record, reservation=reservation)

    claimed, _, _, _ = await registry.claim(
        identity=identity,
        public_session_id=record.public_session_id,
        now=0.5,
    )
    assert claimed is record

    assert await registry.pop_stale(2.0) == []
    busy, retired, error, close_reason = await registry.claim(
        identity=identity,
        public_session_id=record.public_session_id,
        now=2.0,
    )
    assert busy is None
    assert retired is None
    assert error is ShellSessionErrorCode.SESSION_BUSY
    assert close_reason is None

    await registry.release(record)
    assert await registry.pop_stale(2.0) == [(record, "idle_expired")]


@pytest.mark.asyncio
async def test_claimed_record_remains_subject_to_hard_deadline_cleanup() -> None:
    """A live claim cannot extend the command's authorized maximum runtime."""
    registry = _registry(idle_timeout=1.0)
    identity = _identity()
    reservation = await registry.reserve_start(identity)
    assert reservation is not None
    record = _record(
        "shs_claimed_deadline",
        identity=identity,
        last_activity_at=0.0,
        deadline_at=2.0,
    )
    await registry.register(record, reservation=reservation)
    claimed, _, _, _ = await registry.claim(
        identity=identity,
        public_session_id=record.public_session_id,
        now=0.5,
    )
    assert claimed is record

    assert await registry.pop_stale(2.0) == [(record, "deadline_expired")]


@pytest.mark.asyncio
async def test_owner_task_and_stale_selection_remain_tenant_isolated() -> None:
    registry = _registry(owner_limit=5, task_limit=5, idle_timeout=2.0)
    identities = [
        _identity(tenant_id=1, task_id=2, owner="main:a"),
        _identity(tenant_id=1, task_id=2, owner="main:b"),
        _identity(tenant_id=2, task_id=2, owner="main:a"),
    ]
    records: list[ShellSessionRecord] = []
    for index, identity in enumerate(identities):
        reservation = await registry.reserve_start(identity)
        assert reservation is not None
        record = _record(
            f"shs_{index}",
            identity=identity,
            last_activity_at=0.0,
            deadline_at=100.0,
        )
        records.append(record)
        await registry.register(record, reservation=reservation)

    popped_owner = await registry.pop_owner(
        tenant_id=1,
        task_id=2,
        execution_owner_id="main:a",
    )
    assert popped_owner == [records[0]]
    assert await registry.get_capability(
        identity=identities[2],
        public_session_id=records[2].public_session_id,
    ) is ShellCapability.ASSESSMENT

    stale = await registry.pop_stale(3.0)
    assert [record for record, reason in stale if reason == "idle_expired"] == [
        records[1],
        records[2],
    ]
