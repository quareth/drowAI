"""Tests for canonical active task statuses used by concurrency accounting."""

import pytest

from backend.domain.task_lifecycle import TaskStatus, TaskStatusValidator

pytestmark = pytest.mark.execution_plane_non_dind_regression


def test_active_task_statuses_returns_canonical_counting_set() -> None:
    expected = frozenset(
        {
            TaskStatus.CREATED.value,
            TaskStatus.QUEUED.value,
            TaskStatus.STARTING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.PAUSING.value,
            TaskStatus.PAUSED.value,
            TaskStatus.RESUMING.value,
            TaskStatus.STOPPING.value,
        }
    )

    active_statuses = TaskStatus.active_task_statuses()

    assert isinstance(active_statuses, frozenset)
    assert active_statuses == expected


def test_narrow_active_helpers_delegate_to_canonical_set_without_broadening_behavior() -> None:
    canonical = TaskStatus.active_task_statuses()

    assert set(TaskStatus.get_active_statuses()).issubset(canonical)
    assert set(TaskStatus.runtime_active_statuses()).issubset(canonical)
    assert set(TaskStatus.create_name_reservation_statuses()).issubset(canonical)
    assert set(TaskStatus.engagement_archive_block_statuses()).issubset(canonical)

    assert set(TaskStatus.get_active_statuses()) == {
        TaskStatus.QUEUED.value,
        TaskStatus.STARTING.value,
        TaskStatus.RUNNING.value,
    }
    assert set(TaskStatus.runtime_active_statuses()) == {
        TaskStatus.QUEUED.value,
        TaskStatus.STARTING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.PAUSING.value,
        TaskStatus.RESUMING.value,
    }
    assert set(TaskStatus.create_name_reservation_statuses()) == {
        TaskStatus.CREATED.value,
        TaskStatus.QUEUED.value,
        TaskStatus.STARTING.value,
        TaskStatus.RUNNING.value,
    }
    assert set(TaskStatus.engagement_archive_block_statuses()) == {
        TaskStatus.QUEUED.value,
        TaskStatus.STARTING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.PAUSING.value,
        TaskStatus.PAUSED.value,
        TaskStatus.RESUMING.value,
        TaskStatus.STOPPING.value,
    }


def test_status_validator_is_active_matches_runtime_active_statuses() -> None:
    for status in TaskStatus.get_all_statuses():
        expected = status in TaskStatus.runtime_active_statuses()
        assert TaskStatusValidator.is_active(status) is expected
