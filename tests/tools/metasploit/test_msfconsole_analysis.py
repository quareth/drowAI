"""Unit tests for pure Metasploit msfconsole analysis helpers."""

from __future__ import annotations

from agent.tools.exploitation_tools.metasploit.analysis import (
    analyze_msfconsole_metadata,
    analyze_msfconsole_output,
    metasploit_metadata_from_analysis,
)


def test_msfconsole_analysis_preserves_search_metadata_shape(sample_search_output: str) -> None:
    analysis = analyze_msfconsole_output(stdout=sample_search_output, stderr="")
    metadata = metasploit_metadata_from_analysis(analysis)

    assert metadata["parsed_output"]["success"] is True
    assert metadata["sessions_created"] == 0
    assert metadata["modules_loaded"] == []
    assert metadata["errors"] == []
    assert metadata["warnings"] == []
    assert metadata["stderr"] == ""


def test_msfconsole_analysis_marks_session_backed_exploit_success(
    sample_session_output: str,
) -> None:
    analysis = analyze_msfconsole_output(
        stdout=sample_session_output,
        stderr="",
        execution_mode="script",
        mark_exploit_outcome=True,
    )
    metadata = metasploit_metadata_from_analysis(analysis)

    assert metadata["sessions_created"] == 1
    assert metadata["exploit_succeeded"] is True
    assert metadata["execution_mode"] == "script"
    assert metadata["parsed_output"]["sessions"] == [{"id": 1, "type": "meterpreter"}]


def test_msfconsole_metadata_analysis_preserves_failure_signals() -> None:
    analysis = analyze_msfconsole_metadata(
        {
            "parsed_output": {"success": False, "sessions": []},
            "sessions_created": 0,
            "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
            "errors": ["Exploit failed: No target specified"],
            "warnings": ["No payload configured"],
            "stderr": "warning stream",
            "exploit_succeeded": False,
        }
    )

    assert analysis.sessions_created == 0
    assert analysis.modules_loaded == ("exploit/windows/smb/ms17_010_eternalblue",)
    assert analysis.errors == ("Exploit failed: No target specified",)
    assert analysis.warnings == ("No payload configured",)
    assert analysis.stderr_preview == "warning stream"
    assert analysis.exploit_succeeded is False
