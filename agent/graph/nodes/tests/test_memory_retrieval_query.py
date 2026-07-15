"""Pure-function tests for memory retrieval query extraction."""

from __future__ import annotations

from agent.graph.nodes.memory_retrieval import (
    _extract_query_from_working_memory,
    _split_retrieval_limits,
)


def test_extract_query_empty_metadata() -> None:
    assert _extract_query_from_working_memory({}) == ""


def test_extract_query_with_target_and_objective() -> None:
    metadata = {
        "working_memory": {
            "referents": {"intent:target": {"target": "192.168.1.10"}},
            "objective": {"text": "Check open ports"},
        }
    }
    assert _extract_query_from_working_memory(metadata) == "192.168.1.10 Check open ports"


def test_extract_query_target_only() -> None:
    metadata = {
        "working_memory": {
            "referents": {"intent:target": {"target": "example.org"}},
        }
    }
    assert _extract_query_from_working_memory(metadata) == "example.org"


def test_extract_query_objective_only() -> None:
    metadata = {
        "working_memory": {
            "objective": {"text": "Investigate SSH configuration drift"},
        }
    }
    assert _extract_query_from_working_memory(metadata) == "Investigate SSH configuration drift"


def test_extract_query_with_referent_resolution() -> None:
    metadata = {
        "working_memory": {
            "active": {"target_id": "target:server_a"},
            "referents": {
                "server_a": {"target": "10.10.0.7"},
                "intent:target": {"target": "should.not.be.used"},
            },
            "objective": {"text": "unknown"},
        }
    }
    assert _extract_query_from_working_memory(metadata) == "10.10.0.7"


def test_split_retrieval_limits_default_distribution() -> None:
    user_profile_max, task_engagement_max = _split_retrieval_limits(5)
    assert user_profile_max == 3
    assert task_engagement_max == 2


def test_split_retrieval_limits_total_one() -> None:
    user_profile_max, task_engagement_max = _split_retrieval_limits(1)
    assert user_profile_max == 0
    assert task_engagement_max == 1


def test_split_retrieval_limits_total_two() -> None:
    user_profile_max, task_engagement_max = _split_retrieval_limits(2)
    assert user_profile_max == 1
    assert task_engagement_max == 1
