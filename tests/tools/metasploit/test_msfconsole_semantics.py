"""Regression tests for producer-owned Metasploit exploit semantics.

These tests lock exploit-success semantic emission over Metasploit's parsed
metadata only. Search and inspection tools intentionally remain outside the
Knowledge fact producer boundary.
"""

from __future__ import annotations

import json

from agent.tools.exploitation_tools.metasploit.analysis import (
    METASPLOIT_CAPABILITY_FAMILY,
    METASPLOIT_SEMANTIC_SCHEMA_VERSION,
    build_metasploit_semantic_observations,
)
from agent.tools.exploitation_tools.metasploit.msfconsole import (
    MsfInspectModuleArgs,
    MsfInspectModuleTool,
    MsfModuleInspection,
    MsfRunExploitArgs,
    MsfRunExploitTool,
    MsfSearchModulesArgs,
    MsfSearchModulesTool,
)
from runtime_shared.semantic.canonical_keys import (
    build_host_ip_key,
    build_relationship_edge_key,
)
from runtime_shared.semantic.pentest_facts import SemanticFactEnvelope, compile_facts


def _run_args(**overrides: object) -> MsfRunExploitArgs:
    values = {
        "target": "192.168.1.50",
        "module_path": "exploit/windows/smb/ms17_010_eternalblue",
        "rhosts": "192.168.1.50",
        "lhost": "192.168.1.100",
    }
    values.update(overrides)
    return MsfRunExploitArgs(**values)


def test_msfconsole_session_backed_metadata_emits_finding_and_exact_relationship(
    sample_session_output: str,
) -> None:
    tool = MsfRunExploitTool()
    args = _run_args()
    metadata = tool.parse_output(sample_session_output, "", 0, args)

    observations = tool.emit_semantic_observations(
        stdout="ignored raw session text",
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )

    source_key = build_host_ip_key("192.168.1.100")
    target_key = build_host_ip_key("192.168.1.50")
    assert observations == [
        {
            "observation_type": "finding.exploit_succeeded",
            "subject_type": "finding.instance",
            "subject_key": (
                "finding.instance:msfconsole:exploit/windows/smb/ms17_010_eternalblue:"
                "target-192.168.1.50"
            ),
            "payload": {
                "source": "msfconsole",
                "detector_id": "exploit/windows/smb/ms17_010_eternalblue",
                "target_url": "192.168.1.50",
                "target_ip": "192.168.1.50",
                "confidence": "confirmed",
                "session_count": 1,
                "success": True,
                "source_ip": "192.168.1.100",
                "canonical_subject_detector_id": (
                    "msfconsole:exploit/windows/smb/ms17_010_eternalblue"
                ),
                "canonical_subject_target_url": "target-192.168.1.50",
            },
        },
        {
            "observation_type": "relationship.exploits",
            "subject_type": "relationship.edge",
            "subject_key": build_relationship_edge_key(
                source_subject_key=source_key,
                relationship_type="exploits",
                target_subject_key=target_key,
            ),
            "payload": {
                "source_subject_type": "host.ip",
                "source_subject_key": source_key,
                "target_subject_type": "host.ip",
                "target_subject_key": target_key,
                "relationship_type": "exploits",
                "detector_id": "exploit/windows/smb/ms17_010_eternalblue",
                "success": True,
            },
        },
    ]


def test_msfconsole_explicit_success_without_source_ip_emits_finding_only() -> None:
    tool = MsfRunExploitTool()
    args = _run_args(lhost=None)
    output = """
[*] Using exploit/windows/smb/ms17_010_eternalblue
[+] Exploitation completed successfully
msf6 exploit(windows/smb/ms17_010_eternalblue) >
"""
    metadata = tool.parse_output(output, "", 0, args)

    observations = tool.emit_semantic_observations("", "", 0, args, metadata)

    assert metadata["exploit_succeeded"] is True
    assert len(observations) == 1
    assert observations[0]["observation_type"] == "finding.exploit_succeeded"
    assert observations[0]["subject_key"] == (
        "finding.instance:msfconsole:exploit/windows/smb/ms17_010_eternalblue:"
        "target-192.168.1.50"
    )
    assert observations[0]["payload"]["success"] is True
    assert "source_ip" not in observations[0]["payload"]


def test_msfconsole_session_opened_output_supplies_source_when_lhost_omitted(
    sample_session_output: str,
) -> None:
    tool = MsfRunExploitTool()
    args = _run_args(lhost=None)
    metadata = tool.parse_output(sample_session_output, "", 0, args)

    observations = tool.emit_semantic_observations("", "", 0, args, metadata)

    assert [item["observation_type"] for item in observations] == [
        "finding.exploit_succeeded",
        "relationship.exploits",
    ]
    assert observations[0]["payload"]["source_ip"] == "192.168.1.100"
    assert observations[1]["payload"]["source_subject_key"] == build_host_ip_key(
        "192.168.1.100"
    )


def test_msfconsole_session_opened_output_supplies_target_for_cidr_rhosts() -> None:
    output = """
[*] Started reverse TCP handler on 198.51.100.10:4444
[+] Session 1 opened (198.51.100.10:4444 -> 192.0.2.20:49158)
meterpreter >
"""
    tool = MsfRunExploitTool()
    args = _run_args(target="192.0.2.0/24", rhosts="192.0.2.0/24", lhost="198.51.100.10")
    metadata = tool.parse_output(output, "", 0, args)

    observations = tool.emit_semantic_observations("", "", 0, args, metadata)

    assert [item["observation_type"] for item in observations] == [
        "finding.exploit_succeeded",
        "relationship.exploits",
    ]
    assert observations[0]["subject_key"] == (
        "finding.instance:msfconsole:exploit/windows/smb/ms17_010_eternalblue:"
        "target-192.0.2.20"
    )
    assert observations[0]["payload"]["target_ip"] == "192.0.2.20"
    assert observations[1]["payload"]["target_subject_key"] == build_host_ip_key(
        "192.0.2.20"
    )


def test_msfconsole_fail_closed_for_failed_ambiguous_incomplete_and_missing_target(
    sample_session_output: str,
) -> None:
    args = _run_args()
    failed = {
        "parsed_output": {"sessions": []},
        "sessions_created": 0,
        "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
        "exploit_succeeded": False,
    }
    incomplete = {
        "parsed_output": {"sessions": []},
        "sessions_created": 0,
        "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
    }
    missing_target = {
        "parsed_output": {"sessions": [{"id": 1, "type": "meterpreter"}]},
        "sessions_created": 1,
        "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
    }

    assert build_metasploit_semantic_observations(failed, args) == []
    assert build_metasploit_semantic_observations(incomplete, args) == []
    assert build_metasploit_semantic_observations(missing_target, _run_args(target="", rhosts=None)) == []
    assert (
        MsfRunExploitTool().emit_semantic_observations(
            stdout=sample_session_output,
            stderr="",
            exit_code=0,
            args=args,
            metadata=incomplete,
        )
        == []
    )


def test_msfconsole_parse_output_fail_closes_for_failure_and_ambiguous_output() -> None:
    tool = MsfRunExploitTool()
    args = _run_args()
    failed = """
[*] Using exploit/windows/smb/ms17_010_eternalblue
[-] Exploit failed: The connection timed out
msf6 exploit(windows/smb/ms17_010_eternalblue) >
"""
    ambiguous = """
[*] Using exploit/windows/smb/ms17_010_eternalblue
[*] Started reverse TCP handler on 192.168.1.100:4444
[*] Exploit completed, but no session was created.
msf6 exploit(windows/smb/ms17_010_eternalblue) >
"""

    failed_metadata = tool.parse_output(failed, "", 1, args)
    ambiguous_metadata = tool.parse_output(ambiguous, "", 0, args)

    assert failed_metadata["exploit_succeeded"] is False
    assert ambiguous_metadata["exploit_succeeded"] is False
    assert tool.emit_semantic_observations("", "", 1, args, failed_metadata) == []
    assert tool.emit_semantic_observations("", "", 0, args, ambiguous_metadata) == []


def test_msfconsole_search_and_inspect_tools_emit_no_exploit_facts() -> None:
    search = MsfSearchModulesTool()
    inspect = MsfInspectModuleTool()

    search_args = MsfSearchModulesArgs(target="192.0.2.10", search_term="smb")
    inspect_args = MsfInspectModuleArgs(
        target="192.0.2.10",
        module_path="exploit/windows/smb/ms17_010_eternalblue",
        inspection=MsfModuleInspection.INFO,
    )
    search_metadata = search.parse_output("", "", 0, search_args)
    inspect_metadata = inspect.parse_output("", "", 0, inspect_args)

    assert search.emit_semantic_observations("", "", 0, search_args, search_metadata) == []
    assert inspect.emit_semantic_observations("", "", 0, inspect_args, inspect_metadata) == []
    assert search_metadata
    assert inspect_metadata


def test_msfconsole_secret_bearing_options_are_not_leaked_in_semantic_rows(
    sample_session_output: str,
) -> None:
    args = _run_args(
        payload="windows/meterpreter/reverse_tcp",
        lport=4444,
        custom_options={"HttpPassword": "super-secret-password"},
    )
    metadata = MsfRunExploitTool().parse_output(sample_session_output, "", 0, args)

    observations = build_metasploit_semantic_observations(metadata, args)
    encoded = json.dumps(observations, sort_keys=True)

    assert "super-secret-password" not in encoded
    assert "HttpPassword" not in encoded


def test_msfconsole_semantic_observations_compile_directly() -> None:
    args = _run_args()
    rows = build_metasploit_semantic_observations(
        {
            "parsed_output": {"sessions": [{"id": 1, "type": "meterpreter"}]},
            "sessions_created": 1,
            "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
        },
        args,
    )

    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version=METASPLOIT_SEMANTIC_SCHEMA_VERSION,
            capability_family=METASPLOIT_CAPABILITY_FAMILY,
            observations=tuple(rows),
            evidence=(),
        )
    )

    assert compiled.accepted_count == 2
    assert compiled.rejected_count == 0
    assert compiled.diagnostics == ()


def test_msfconsole_run_exploit_parse_output_stamps_semantic_transport(
    sample_session_output: str,
) -> None:
    metadata = MsfRunExploitTool().parse_output(sample_session_output, "", 0, _run_args())

    assert metadata["semantic_schema_version"] == METASPLOIT_SEMANTIC_SCHEMA_VERSION
    assert metadata["capability_family"] == METASPLOIT_CAPABILITY_FAMILY
