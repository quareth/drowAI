"""Unit tests for Metasploit deterministic compression helpers."""

from __future__ import annotations

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.metasploit import (
    MSF_INSPECT_MODULE_TOOL_ID,
    MSF_RUN_EXPLOIT_TOOL_ID,
    MSF_SEARCH_MODULES_TOOL_ID,
    metasploit_adapter,
    registered_metasploit_tool_ids,
)
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)


def test_metasploit_adapter_registers_visible_msfconsole_tools() -> None:
    """Visible narrow Metasploit tools resolve to the deterministic adapter."""

    assert registered_metasploit_tool_ids() == (
        MSF_SEARCH_MODULES_TOOL_ID,
        MSF_INSPECT_MODULE_TOOL_ID,
        MSF_RUN_EXPLOIT_TOOL_ID,
    )
    assert get_adapter(MSF_SEARCH_MODULES_TOOL_ID) is metasploit_adapter
    assert get_adapter(MSF_INSPECT_MODULE_TOOL_ID) is metasploit_adapter
    assert get_adapter(MSF_RUN_EXPLOIT_TOOL_ID) is metasploit_adapter


def test_metasploit_search_metadata_compacts_existing_facts() -> None:
    result = compress_deterministically(
        CompressionInput(
            tool_name=MSF_SEARCH_MODULES_TOOL_ID,
            raw_result={
                "metadata": {
                    "parsed_output": {"success": True, "sessions": []},
                    "sessions_created": 0,
                    "modules_loaded": [],
                    "exploits_executed": [],
                    "errors": [],
                    "warnings": [],
                    "stderr": "",
                    "execution_mode": "script",
                },
                "artifacts": ["artifacts/msfconsole_search.txt"],
            },
        )
    )

    assert result.summary == (
        "Metasploit search_modules parsed msfconsole output; sessions=0, modules=0."
    )
    assert result.key_findings == (
        "execution mode: script",
        "sessions created: 0",
        "artifact: artifacts/msfconsole_search.txt",
    )
    assert result.structured_signals[:4] == (
        {"type": "kv_pair", "key": "metasploit_tool", "value": "search_modules"},
        {"type": "kv_pair", "key": "metasploit_sessions_created", "value": 0},
        {"type": "kv_pair", "key": "metasploit_modules_loaded_count", "value": 0},
        {"type": "kv_pair", "key": "metasploit_execution_mode", "value": "script"},
    )


def test_metasploit_inspect_metadata_reports_loaded_module() -> None:
    result = compress_deterministically(
        CompressionInput(
            tool_name=MSF_INSPECT_MODULE_TOOL_ID,
            raw_result={
                "metadata": {
                    "parsed_output": {"success": True, "sessions": []},
                    "sessions_created": 0,
                    "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
                    "exploits_executed": [],
                    "errors": [],
                    "warnings": [],
                    "stderr": "",
                }
            },
        )
    )

    assert "modules=1" in result.summary
    assert "modules loaded: exploit/windows/smb/ms17_010_eternalblue" in result.key_findings
    assert (
        "metasploit module: exploit/windows/smb/ms17_010_eternalblue"
        in result.decision_evidence
    )


def test_metasploit_run_exploit_success_and_failure() -> None:
    success = compress_deterministically(
        CompressionInput(
            tool_name=MSF_RUN_EXPLOIT_TOOL_ID,
            raw_result={
                "metadata": {
                    "parsed_output": {
                        "success": True,
                        "sessions": [{"id": 1, "type": "shell"}],
                    },
                    "sessions_created": 1,
                    "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
                    "exploits_executed": [{"sessions_created": 1}],
                    "errors": [],
                    "warnings": [],
                    "stderr": "",
                    "exploit_succeeded": True,
                }
            },
        )
    )
    failure = compress_deterministically(
        CompressionInput(
            tool_name=MSF_RUN_EXPLOIT_TOOL_ID,
            raw_result={
                "metadata": {
                    "parsed_output": {"success": False, "sessions": []},
                    "sessions_created": 0,
                    "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
                    "exploits_executed": [],
                    "errors": ["Exploit failed: No target specified"],
                    "warnings": [],
                    "stderr": "",
                    "exploit_succeeded": False,
                }
            },
        )
    )

    assert success.summary == (
        "Metasploit run_exploit succeeded; sessions=1, modules=1."
    )
    assert "exploit_succeeded: true" in success.key_findings
    assert {
        "type": "kv_pair",
        "key": "metasploit_exploit_succeeded",
        "value": True,
    } in success.structured_signals

    assert failure.summary == (
        "Metasploit run_exploit did not create a session; sessions=0, modules=1."
    )
    assert "exploit_succeeded: false" in failure.key_findings
    assert failure.errors == ("Exploit failed: No target specified",)
