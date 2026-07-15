"""Unit tests for shared observation section parsing and merge helpers."""

from backend.services.chat.observation_sections import (
    merge_observation_tokens,
    parse_observation_sections,
)


def test_parse_observation_sections_empty_mode_filters_non_list_payloads() -> None:
    assert parse_observation_sections(None, non_list_strategy="empty") == []
    assert parse_observation_sections("", non_list_strategy="empty") == []
    assert parse_observation_sections('{"content":"x"}', non_list_strategy="empty") == []
    assert parse_observation_sections("not-json", non_list_strategy="empty") == []


def test_parse_observation_sections_raw_mode_wraps_non_list_as_raw_text() -> None:
    assert parse_observation_sections("not-json", non_list_strategy="raw") == ["not-json"]
    assert parse_observation_sections('{"content":"x"}', non_list_strategy="raw") == [
        '{"content":"x"}'
    ]


def test_parse_observation_sections_dict_or_raw_mode_preserves_dict_payloads() -> None:
    assert parse_observation_sections(
        '{"content":"x"}',
        non_list_strategy="dict_or_raw",
    ) == [{"content": "x"}]
    assert parse_observation_sections("42", non_list_strategy="dict_or_raw") == ["42"]


def test_parse_observation_sections_dict_only_filters_to_dict_items() -> None:
    assert parse_observation_sections(
        '[{"content":"a"}, "b", 7, {"content":"c"}]',
        non_list_strategy="raw",
        dict_only=True,
    ) == [{"content": "a"}, {"content": "c"}]


def test_merge_observation_tokens_clears_when_incoming_is_none() -> None:
    assert merge_observation_tokens('["existing"]', None) is None


def test_merge_observation_tokens_keeps_existing_when_incoming_is_empty() -> None:
    assert merge_observation_tokens('["existing"]', "") == '["existing"]'


def test_merge_observation_tokens_uses_incoming_when_existing_is_empty() -> None:
    assert merge_observation_tokens(None, '["incoming"]') == '["incoming"]'


def test_merge_observation_tokens_keeps_incoming_prefix_superset() -> None:
    assert (
        merge_observation_tokens('["obs-one"]', '["obs-one", "obs-two"]')
        == '["obs-one", "obs-two"]'
    )


def test_merge_observation_tokens_keeps_existing_prefix_superset() -> None:
    assert (
        merge_observation_tokens('["obs-one", "obs-two"]', '["obs-one"]')
        == '["obs-one", "obs-two"]'
    )


def test_merge_observation_tokens_dedups_tail_item_when_appending() -> None:
    assert (
        merge_observation_tokens('["obs-one", "obs-two"]', '["obs-two", "obs-three"]')
        == '["obs-one", "obs-two", "obs-three"]'
    )


def test_merge_observation_tokens_wraps_raw_non_list_payloads() -> None:
    assert merge_observation_tokens(None, "raw observation") == '["raw observation"]'
