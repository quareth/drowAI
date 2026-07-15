"""Prompt policy tests for task-scoped artifact retrieval guidance."""

from __future__ import annotations

from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
from core.prompts.builders.tool_planning import ToolPlanningPromptBuilder


def test_post_tool_system_prompt_hides_artifact_db_tools() -> None:
    prompt = PostToolReasoningPromptBuilder().build_system_prompt()
    assert "artifact.search" not in prompt
    assert "artifact.read" not in prompt
    assert "Saved Evidence Policy" in prompt
    assert "Artifact database lookup tools are internal" in prompt
    assert "Saved evidence read rules" in prompt
    assert "compressed output from the previous read is authoritative" in prompt
    assert "Only read the same file again if the previous read explicitly failed" in prompt


def test_deep_reasoning_prompts_hide_artifact_db_tools() -> None:
    builder = DeepReasoningPromptBuilder()
    state = {"facts": {"plan": [], "runtime_budgets": {}, "tool_ids": []}, "trace": {}}
    system_prompt = builder.build_system_prompt(state)
    decision_prompt = builder.build_decision_prompt(state)
    assert "Artifact Retrieval Policy" in system_prompt
    assert "artifact.search" not in system_prompt
    assert "artifact.read" not in system_prompt
    assert "Artifact database lookup tools are internal" in system_prompt
    assert "search-before-read policy" not in decision_prompt


def test_tool_planning_select_prompt_filters_artifact_tools_from_prompt() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_select_tools_prompt(
        resolved_tools=["filesystem.read_file", "artifact.search", "artifact.read"],
        catalog=[
            {"id": "filesystem.read_file", "name": "filesystem.read_file", "description": "read files"},
            {"id": "artifact.search", "name": "artifact.search", "description": "search artifacts"},
            {"id": "artifact.read", "name": "artifact.read", "description": "read artifacts"},
        ],
        target="10.0.0.1",
        phase="enumeration",
        constraints={},
    )
    assert "Artifact Tool Policy:" not in prompt
    assert "artifact.search" not in prompt
    assert "artifact.read" not in prompt
    assert "filesystem.read_file" in prompt


def test_tool_planning_select_prompt_omits_artifact_policy_when_not_visible() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_select_tools_prompt(
        resolved_tools=["shell.exec"],
        catalog=[{"id": "shell.exec", "name": "shell.exec", "description": "run shell"}],
        target="10.0.0.1",
        phase="enumeration",
        constraints={},
    )
    assert "Artifact Tool Policy:" not in prompt


def test_tool_parameters_prompt_includes_filesystem_runbook_for_read_file() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["filesystem.read_file"],
        target="artifacts/scan.xml",
        phase="enumeration",
        constraints={},
    )

    assert "Tool Runbooks:" in prompt
    assert "Runbook: artifact-evidence-reading" in prompt
    assert "# Artifact Evidence Reading" in prompt
    assert "## Parameter Safety Rules" in prompt
    assert "## Workflow" in prompt
    assert "## Artifact Playbooks" in prompt
    assert "## Bad Calls" in prompt
    assert "not to decide whether artifact reading should happen" in prompt
    assert "Full-file reads are allowed only when metadata shows the file is small" in prompt
    assert "confirm presence, confirm absence, or expose the next useful slice" in prompt
    assert "a no-match result is valid evidence" in prompt
    assert "Nmap XML or text" in prompt
    assert "Gobuster or ffuf output" in prompt
    assert "Hashcat output" in prompt
    assert "Tcpdump text" in prompt
    assert "Do not call filesystem.read_file with read_mode=\"full\"" in prompt
    assert "If the compact observation already answers" not in prompt
    assert "do not read an artifact" not in prompt
    assert "After finding candidate lines" not in prompt


def test_tool_parameters_prompt_includes_filesystem_runbook_for_search_text() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["filesystem.search_text"],
        target="artifacts/scan.xml",
        phase="enumeration",
        constraints={},
    )

    assert "Tool Runbooks:" in prompt
    assert "Runbook: artifact-evidence-reading" in prompt
    assert "Prefer filesystem.search_text when there is a known literal" in prompt
    assert "to test for" in prompt


def test_tool_parameters_prompt_omits_filesystem_runbook_for_other_tools() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["nmap.scan"],
        target="10.0.0.5",
        phase="enumeration",
        constraints={},
    )

    assert "Tool Runbooks:" not in prompt
    assert "Runbook: artifact-evidence-reading" not in prompt
    assert "# Artifact Evidence Reading" not in prompt


def test_tool_parameters_prompt_includes_metasploit_runbook_for_split_tools() -> None:
    builder = ToolPlanningPromptBuilder()

    for tool_id in (
        "exploitation_tools.metasploit.search_modules",
        "exploitation_tools.metasploit.inspect_module",
        "exploitation_tools.metasploit.run_exploit",
    ):
        prompt = builder.build_tool_parameters_prompt(
            selected_tools=[tool_id],
            target="cve-2018-7600-web-1",
            phase="exploitation",
            constraints={},
        )

        assert "Tool Runbooks:" in prompt
        assert "Runbook: metasploit-exploitation" in prompt
        assert "# Metasploit Exploitation" in prompt
        assert "Metasploit is a module console" in prompt
        assert "Removed broad tool id: `exploitation_tools.metasploit.msfconsole`" in prompt
        assert "Version labels such as `Drupal 7`, `Drupal 8`" in prompt
        assert "session creation is the primary success signal" in prompt


def test_tool_planning_select_prompt_never_injects_filesystem_runbook() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_select_tools_prompt(
        resolved_tools=["filesystem.read_file", "filesystem.search_text"],
        catalog=[
            {
                "id": "filesystem.read_file",
                "name": "filesystem.read_file",
                "description": "read files",
            },
            {
                "id": "filesystem.search_text",
                "name": "filesystem.search_text",
                "description": "search files",
            },
        ],
        target="artifacts/scan.xml",
        phase="enumeration",
        constraints={},
    )

    assert "Tool Runbooks:" not in prompt
    assert "Runbook: artifact-evidence-reading" not in prompt
    assert "# Artifact Evidence Reading" not in prompt


def test_tool_planning_select_prompt_injects_web_runbook_for_web_category() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_select_tools_prompt(
        resolved_tools=[
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
            "web_applications.web_crawlers.ffuf",
        ],
        catalog=[
            {
                "id": "information_gathering.web_enumeration.http_request",
                "name": "information_gathering.web_enumeration.http_request",
                "description": "fetch one URL",
            },
            {
                "id": "information_gathering.web_enumeration.http_download",
                "name": "information_gathering.web_enumeration.http_download",
                "description": "download one URL",
            },
            {
                "id": "web_applications.web_crawlers.ffuf",
                "name": "web_applications.web_crawlers.ffuf",
                "description": "discover paths",
            },
        ],
        selected_categories=["web_applications"],
        target="http://example.test",
        phase="enumeration",
        constraints={},
    )

    assert "Tool Runbooks:" in prompt
    assert "Runbook: web-discovery" in prompt
    assert "Choose tools by the shape of the work" in prompt
    assert "select `web_applications.web_crawlers.ffuf` rather than only `http_request`" in prompt
    assert "The target must contain a path fuzz marker" not in prompt


def test_tool_parameters_prompt_injects_web_parameter_section_for_web_tool() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["web_applications.web_crawlers.ffuf"],
        target="http://example.test/FUZZ",
        phase="enumeration",
        constraints={},
    )

    assert "Tool Runbooks:" in prompt
    assert "Runbook: ffuf-crawler" in prompt
    assert "The `FUZZ` marker must be in the URL path" in prompt
    assert "Choose tools by the shape of the work" not in prompt


def test_tool_parameters_prompt_renders_artifact_file_metadata() -> None:
    builder = ToolPlanningPromptBuilder()
    prompt = builder.build_tool_parameters_prompt(
        selected_tools=["filesystem.read_file"],
        target="artifacts/scan.xml",
        phase="enumeration",
        constraints={},
        artifact_file_metadata=[
            {
                "path": "artifacts/scan.xml",
                "status": "ready",
                "size_bytes": 128,
                "line_count": 7,
            },
            {
                "path": "artifacts/missing.xml",
                "status": "unavailable",
                "reason": "file does not exist",
            },
        ],
    )

    assert "Artifact File Metadata:" in prompt
    assert "path=artifacts/scan.xml; status=ready; size_bytes=128; line_count=7" in prompt
    assert "path=artifacts/missing.xml; status=unavailable; reason=file does not exist" in prompt
    assert prompt.index("Tool Runbooks:") < prompt.index("Artifact File Metadata:")


def test_post_tool_user_prompt_adds_selective_cve_lookup_guidance_when_visible() -> None:
    builder = PostToolReasoningPromptBuilder()
    interactive = {
        "facts": {
            "message": "Continue evidence triage",
            "capability": "deep_reasoning",
            "metadata": {
                "tool_catalog": {"entries": [{"tool_id": "knowledge.cve_lookup"}]},
                "last_tool_result": {
                    "parameters": {},
                    "was_truncated": False,
                    "chars_truncated": 0,
                    "suggest_file_reading": False,
                },
                "last_tool_result_compact": {
                    "summary": "Service fingerprint identified.",
                    "key_findings": ["apache httpd 2.4.58"],
                    "errors": [],
                },
            },
        }
    }

    prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized={"tool": "shell.exec", "summary": "ok", "key_findings": []},
    )

    assert "Selective CVE Lookup Guidance" in prompt
    assert "optional enrichment" in prompt
    assert "Do NOT call it after every tool by default" in prompt
    assert "authoritative finding evidence is already present" in prompt
    assert "prefer `source_artifact_id` from Artifact References" in prompt
    assert "high (>=0.80)" in prompt


def test_post_tool_user_prompt_omits_selective_cve_lookup_guidance_when_not_visible() -> None:
    builder = PostToolReasoningPromptBuilder()
    interactive = {
        "facts": {
            "message": "Continue evidence triage",
            "capability": "deep_reasoning",
            "metadata": {
                "tool_catalog": {"entries": [{"tool_id": "shell.exec"}]},
                "last_tool_result": {
                    "parameters": {},
                    "was_truncated": False,
                    "chars_truncated": 0,
                    "suggest_file_reading": False,
                },
                "last_tool_result_compact": {
                    "summary": "Service fingerprint identified.",
                    "key_findings": ["apache httpd 2.4.58"],
                    "errors": [],
                },
            },
        }
    }

    prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized={"tool": "shell.exec", "summary": "ok", "key_findings": []},
    )

    assert "Selective CVE Lookup Guidance" not in prompt
