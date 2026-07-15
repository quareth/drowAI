"""Contract tests for tool output processing v4 templates."""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS = _ROOT / "core" / "prompts" / "versions" / "tool_output_processing"
_SEMANTIC_BLOCK = """SEMANTIC EVIDENCE HANDLING
- `semantic_evidence` is deterministic execution-local context produced by the tool layer.
- Treat both `semantic_evidence` and the raw output as observed facts from independent sources.
- If they conflict, do not silently prefer either: preserve both, record the conflict verbatim in `decision_evidence`, downgrade `lossiness_risk` to at least `medium`, and avoid absolute claims in `summary` and `key_findings`.
- Conflicts are signal, not noise; they may indicate a parser bug or runtime anomaly.
- `semantic_evidence` is grouped by `type`. Do not invent new types in `structured_signals`.
- Do not assume `semantic_evidence` is complete. Absence of an evidence entry does not imply absence of the underlying fact.
"""
_BASE_CONTEXT = {
    "tool_call": "tool.exec(arg='x')",
    "output_format": "text",
    "tool_intent": "none",
    "output_mode": "command_output",
    "sampling_mode": "full_or_contiguous",
    "tool_output": "sample output",
}


def _read_template(version: str, name: str) -> str:
    return (_PROMPTS / version / name).read_text(encoding="utf-8")


def _render_v4(template: str, *, semantic_observations: str = "", semantic_evidence: str = "") -> str:
    rules_block = f"{_SEMANTIC_BLOCK}\n" if (semantic_observations or semantic_evidence) else ""
    sections = []
    semantic_lines = []
    if rules_block:
        semantic_lines.append(rules_block.strip())
    if semantic_observations:
        if semantic_lines:
            semantic_lines.append("")
        semantic_lines.extend(["SEMANTIC OBSERVATIONS:", semantic_observations])
    if semantic_evidence:
        if semantic_lines:
            semantic_lines.append("")
        semantic_lines.extend(["SEMANTIC EVIDENCE:", semantic_evidence])
    if semantic_lines:
        sections.append("\n".join(semantic_lines))
    context = dict(_BASE_CONTEXT)
    context.update(
        {
            "analysis_context": "\n\n".join(sections) + "\n\n" if sections else "",
        }
    )
    return template.format_map(context)


def test_v4_success_template_empty_semantic_inputs_has_raw_output_boundary() -> None:
    v4 = _read_template("v4", "success.txt")
    rendered = _render_v4(v4)

    assert "RAW TOOL OUTPUT TO ANALYZE:\nsample output" in rendered
    assert "{analysis_context}" not in rendered


def test_v4_failure_template_empty_semantic_inputs_has_raw_output_boundary() -> None:
    v4 = _read_template("v4", "failure.txt")
    rendered = _render_v4(v4)

    assert "RAW TOOL OUTPUT TO ANALYZE (contains errors):\nsample output" in rendered
    assert "{analysis_context}" not in rendered


def test_v4_templates_include_semantic_rules_block_when_semantics_present() -> None:
    success_template = _read_template("v4", "success.txt")
    failure_template = _read_template("v4", "failure.txt")

    rendered_success = _render_v4(
        success_template,
        semantic_observations='{"observation_type":"web.path_discovered"}',
        semantic_evidence='{"result_summary":[{"name":"results_count","type":"result_summary","value":0}]}',
    )
    rendered_failure = _render_v4(
        failure_template,
        semantic_observations='{"observation_type":"web.path_discovered"}',
        semantic_evidence='{"result_summary":[{"name":"results_count","type":"result_summary","value":0}]}',
    )

    assert _SEMANTIC_BLOCK in rendered_success
    assert _SEMANTIC_BLOCK in rendered_failure
    assert "SEMANTIC OBSERVATIONS:" in rendered_success
    assert "SEMANTIC EVIDENCE:" in rendered_success
    assert "\n\nRAW TOOL OUTPUT TO ANALYZE:" in rendered_success
    assert "\n\nRAW TOOL OUTPUT TO ANALYZE (contains errors):" in rendered_failure


def test_v4_templates_separate_analysis_context_from_raw_output() -> None:
    success_template = _read_template("v4", "success.txt")

    context = dict(_BASE_CONTEXT)
    context["analysis_context"] = (
        "ANALYSIS CONTEXT:\n"
        '{"summary":"derived fact"}'
        "\n\n"
    )
    rendered = success_template.format_map(context)

    assert "ANALYSIS CONTEXT:\n{\"summary\":\"derived fact\"}" in rendered
    assert "}\n\nRAW TOOL OUTPUT TO ANALYZE:\nsample output" in rendered


def test_tool_output_templates_preserve_distinct_warnings_and_errors() -> None:
    success_template = _read_template("v4", "success.txt")
    failure_template = _read_template("v4", "failure.txt")

    expected = (
        "preserve every distinct warning/error/failure/status line that changes "
        "interpretation or next action"
    )
    no_collapse = "Do not collapse multiple distinct warnings/errors into one generic finding"

    assert expected in success_template.lower()
    assert expected in failure_template.lower()
    assert no_collapse in success_template
    assert no_collapse in failure_template
