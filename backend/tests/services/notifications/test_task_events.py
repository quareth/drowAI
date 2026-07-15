"""Tests for reusable task notification stream event builders."""

from backend.services.notifications.task_events import build_projection_notification_event


def test_projection_notification_event_uses_generic_task_notification_shape() -> None:
    event = build_projection_notification_event(
        task_id=12,
        engagement_id=7,
        ingestion_run_id="run-1",
        source_execution_id="exec-1",
        tool_name="nmap",
        asset_insert_count=2,
        finding_insert_count=1,
    )

    assert event is not None
    assert event["type"] == "status"
    assert event["content"] == "task_notification"
    metadata = event["metadata"]
    assert metadata["category"] == "knowledge_delta"
    assert metadata["task_id"] == 12
    assert metadata["engagement_id"] == 7
    assert metadata["asset_insert_count"] == 2
    assert metadata["finding_insert_count"] == 1


def test_projection_notification_event_skips_zero_insert_counts() -> None:
    event = build_projection_notification_event(
        task_id=12,
        engagement_id=7,
        ingestion_run_id="run-1",
        source_execution_id="exec-1",
        tool_name="nmap",
        asset_insert_count=0,
        finding_insert_count=0,
    )

    assert event is None
