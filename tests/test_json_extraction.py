"""Tests for shared JSON extraction utility used across LLM parsing paths."""

from core.llm.json_extraction import extract_json_object


def test_extract_json_object_from_pure_json() -> None:
    content = '{"selected_tools":["shell.exec"],"execution_strategy":"sequential"}'
    parsed = extract_json_object(content)

    assert parsed["selected_tools"] == ["shell.exec"]
    assert parsed["execution_strategy"] == "sequential"


def test_extract_json_object_from_markdown_code_block() -> None:
    content = """```json
{"selected_tools":["http.request"],"execution_strategy":"parallel"}
```"""
    parsed = extract_json_object(content)

    assert parsed["selected_tools"] == ["http.request"]
    assert parsed["execution_strategy"] == "parallel"


def test_extract_json_object_from_embedded_prose() -> None:
    content = (
        "I recommend this plan:\n"
        '{"selected_tools":["tool.one","tool.two"],"execution_strategy":"sequential"}\n'
        "These tools should work."
    )
    parsed = extract_json_object(content)

    assert parsed["selected_tools"] == ["tool.one", "tool.two"]
    assert parsed["execution_strategy"] == "sequential"


def test_extract_json_object_handles_quoted_braces() -> None:
    content = (
        'Prefix text {"note":"brace here: {not-an-object}",'
        '"selected_tools":["shell.exec"]} trailing text'
    )
    parsed = extract_json_object(content)

    assert parsed["selected_tools"] == ["shell.exec"]
    assert parsed["note"] == "brace here: {not-an-object}"


def test_extract_json_object_returns_empty_dict_for_invalid_or_missing_json() -> None:
    assert extract_json_object("no json here") == {}
    assert extract_json_object("```json\n{invalid json}\n```") == {}
