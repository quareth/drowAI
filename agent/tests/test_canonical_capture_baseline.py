"""Baseline regression tests for the canonical capture migration (Phase 0).

These tests freeze the current metadata and artifact behavior of the six
runtime_ingestion knowledge-backed tools before any capture-contract changes.
They verify:
- metadata keys consumed by current adapters
- artifact presence when adapters depend on fallback evidence
- silent metadata-loss scenarios (format drift)

Every test in this file must pass BEFORE any Phase 1+ changes are applied.
If a migration task breaks a test here, the migration must be fixed --
not the baseline."""

from __future__ import annotations

import subprocess
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from agent.tools.information_gathering.network_discovery.nmap import (
    NmapArgs,
    NmapTool,
    parse_nmap_xml,
)
from agent.tools.information_gathering.network_discovery.masscan import (
    MasscanTool,
    parse_masscan_json,
)
from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
    NucleiArgs,
    NucleiMode,
    NucleiTool,
    ReportFormat,
    parse_nuclei_json,
    parse_nuclei_text,
)
from agent.tools.web_applications.web_vulnerability_scanners.sqlmap import (
    SqlmapArgs,
    SqlmapTool,
    parse_sqlmap_json,
    parse_sqlmap_text,
)
from agent.tools.web_applications.web_crawlers.gobuster import (
    GobusterArgs,
    GobusterTool,
)
from agent.tools.exploitation_tools.metasploit.msfconsole import (
    MsfRunExploitArgs,
    MsfRunExploitTool,
    parse_msfconsole_output,
)

from backend.services.knowledge.adapters.nmap_adapter import NmapKnowledgeAdapter
from backend.services.knowledge.adapters.masscan_adapter import MasscanKnowledgeAdapter
from backend.services.knowledge.adapters.nuclei_adapter import NucleiKnowledgeAdapter
from backend.services.knowledge.adapters.sqlmap_adapter import SqlmapKnowledgeAdapter
from backend.services.knowledge.adapters.gobuster_adapter import GobusterKnowledgeAdapter
from backend.services.knowledge.adapters.msfconsole_adapter import MsfconsoleKnowledgeAdapter
from backend.services.knowledge.adapters.base import AdapterContext


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _adapter_context(
    *,
    tool_name: str,
    tool_metadata: dict | None = None,
    artifacts: list[dict] | None = None,
    tool_arguments: dict | None = None,
) -> AdapterContext:
    execution_metadata: dict = {"tool_metadata": tool_metadata or {}}
    execution_payload = {
        "execution": {
            "execution_id": "baseline-exec-1",
            "tool_name": tool_name,
            "tool_arguments": tool_arguments or {},
            "execution_metadata": execution_metadata,
        },
        "artifacts": artifacts or [],
    }
    return AdapterContext(
        user_id=1,
        engagement_id=1,
        task_id=2,
        source_execution_id="baseline-exec-1",
        ingestion_run_id="baseline-run-1",
        execution_payload=execution_payload,
        tool_metadata=tool_metadata or {},
        semantic_observations=[],
        artifact_summaries=artifacts or [],
    )


# ---------------------------------------------------------------------------
# Nmap baseline
# ---------------------------------------------------------------------------

NMAP_XML = """<?xml version='1.0'?>
<nmaprun>
  <runstats><hosts up='1' down='0' total='1'/></runstats>
  <host>
    <address addr='10.0.0.1' addrtype='ipv4'/>
    <status state='up'/>
    <ports>
      <port protocol='tcp' portid='22'>
        <state state='open'/>
        <service name='ssh' product='OpenSSH' version='8.9p1'/>
      </port>
      <port protocol='tcp' portid='80'>
        <state state='open'/>
        <service name='http' product='nginx' version='1.18.0'/>
      </port>
    </ports>
  </host>
</nmaprun>
"""


class TestNmapBaseline:
    """Freeze nmap metadata keys and artifact behavior."""

    def test_parse_output_xml_returns_required_metadata_keys(self) -> None:
        metadata = parse_nmap_xml(NMAP_XML)
        assert "open_ports" in metadata
        assert "hosts" in metadata
        assert "hosts_up" in metadata
        assert "hosts_total" in metadata
        assert len(metadata["open_ports"]) == 2
        for port in metadata["open_ports"]:
            assert "port" in port
            assert "protocol" in port
            assert "service" in port

    def test_parse_output_xml_preserves_product_and_version(self) -> None:
        metadata = parse_nmap_xml(NMAP_XML)
        by_port = {p["port"]: p for p in metadata["open_ports"]}
        assert by_port[22]["product"] == "OpenSSH"
        assert by_port[22]["version"] == "8.9p1"
        assert by_port[80]["product"] == "nginx"

    def test_build_command_always_includes_oX_flag(self) -> None:
        tool = NmapTool()
        args = NmapArgs(target="10.0.0.1")
        cmd = tool.build_command(args)
        assert "-oX" in cmd
        assert "-" in cmd[cmd.index("-oX") + 1:]

    def test_parse_output_always_parses_xml(self) -> None:
        """After canonical capture migration, parse_output always extracts XML."""
        tool = NmapTool()
        args = NmapArgs(target="10.0.0.1")
        metadata = tool.parse_output(NMAP_XML, "", 0, args)
        assert len(metadata["open_ports"]) == 2

    def test_adapter_extracts_from_tool_metadata_hosts_key(self) -> None:
        adapter = NmapKnowledgeAdapter()
        context = _adapter_context(
            tool_name="information_gathering.network_discovery.nmap",
            tool_metadata={
                "hosts": [
                    {
                        "ip": "10.0.0.1",
                        "ports": [
                            {"port": 22, "protocol": "tcp", "service": "ssh"},
                        ],
                    }
                ]
            },
        )
        observations = adapter.extract(context)
        types = {o.observation_type for o in observations}
        assert "network.host_discovered" in types
        assert "network.open_port" in types
        assert "network.service_detected" in types

    def test_adapter_falls_back_to_artifact_text_when_metadata_empty(self) -> None:
        adapter = NmapKnowledgeAdapter()
        nmap_text_output = (
            "Nmap scan report for 10.0.0.1\n"
            "22/tcp open ssh OpenSSH 8.9p1\n"
        )
        context = _adapter_context(
            tool_name="information_gathering.network_discovery.nmap",
            tool_metadata={},
            artifacts=[{"artifact_id": "a1", "artifact_kind": "stdout", "content_text": nmap_text_output}],
        )
        observations = adapter.extract(context)
        host_obs = [o for o in observations if o.observation_type == "network.host_discovered"]
        port_obs = [o for o in observations if o.observation_type == "network.open_port"]
        assert len(host_obs) >= 1
        assert len(port_obs) >= 1


# ---------------------------------------------------------------------------
# Masscan baseline
# ---------------------------------------------------------------------------

MASSCAN_JSON = '[{"ip":"10.0.0.1","ports":[{"port":80,"proto":"tcp","status":"open","service":"http"}]}]'


class TestMasscanBaseline:
    """Freeze masscan metadata keys and artifact behavior."""

    def test_parse_output_json_returns_required_metadata_keys(self) -> None:
        metadata = parse_masscan_json(MASSCAN_JSON)
        assert "open_ports" in metadata
        assert "hosts" in metadata
        assert len(metadata["open_ports"]) == 1
        port = metadata["open_ports"][0]
        assert port["port"] == 80
        assert port["protocol"] == "tcp"

    def test_build_command_always_includes_oJ_flag(self) -> None:
        tool = MasscanTool()
        from agent.tools.information_gathering.network_discovery.masscan import MasscanArgsV2
        args = MasscanArgsV2(target="10.0.0.1", ports="80")
        cmd = tool.build_command(args)
        assert "-oJ" in cmd

    def test_parse_output_always_parses_json(self) -> None:
        """After canonical capture migration, parse_output always extracts JSON."""
        tool = MasscanTool()
        from agent.tools.information_gathering.network_discovery.masscan import MasscanArgsV2
        args = MasscanArgsV2(target="10.0.0.1", ports="80")
        metadata = tool.parse_output(MASSCAN_JSON, "", 0, args)
        assert len(metadata["open_ports"]) == 1

    def test_adapter_extracts_host_and_port_from_metadata_single_host(self) -> None:
        """Masscan adapter maps ports to single-host IP when open_ports lack ip field
        and artifact text provides the ip-resolved port entries."""
        adapter = MasscanKnowledgeAdapter()
        context = _adapter_context(
            tool_name="information_gathering.network_discovery.masscan",
            tool_metadata={
                "hosts": [{"ip": "10.0.0.1"}],
                "open_ports": [{"port": 80, "protocol": "tcp", "status": "open"}],
            },
            artifacts=[{"artifact_id": "a1", "artifact_kind": "stdout", "content_text": MASSCAN_JSON}],
        )
        observations = adapter.extract(context)
        types = {o.observation_type for o in observations}
        assert "network.host_discovered" in types
        assert "network.open_port" in types

    def test_adapter_falls_back_to_artifact_json_when_metadata_has_no_ip(self) -> None:
        adapter = MasscanKnowledgeAdapter()
        context = _adapter_context(
            tool_name="information_gathering.network_discovery.masscan",
            tool_metadata={
                "hosts": [{"ip": "10.0.0.1"}],
                "open_ports": [{"port": 80, "protocol": "tcp", "status": "open"}],
            },
            artifacts=[{"artifact_id": "a1", "artifact_kind": "stdout", "content_text": MASSCAN_JSON}],
        )
        observations = adapter.extract(context)
        port_obs = [o for o in observations if o.observation_type == "network.open_port"]
        assert len(port_obs) >= 1


# ---------------------------------------------------------------------------
# Nuclei baseline
# ---------------------------------------------------------------------------

NUCLEI_JSONL = '{"template-id":"CVE-2021-44228","severity":"critical","matched-at":"http://example.com"}\n'


class TestNucleiBaseline:
    """Freeze nuclei metadata keys and artifact behavior."""

    def test_parse_nuclei_json_returns_results_key(self) -> None:
        metadata = parse_nuclei_json(NUCLEI_JSONL)
        assert "results" in metadata
        assert len(metadata["results"]) == 1
        assert metadata["results"][0]["template-id"] == "CVE-2021-44228"

    def test_build_command_always_includes_jsonl_flag(self) -> None:
        tool = NucleiTool()
        args = NucleiArgs(target="http://example.com")
        cmd = tool.build_command(args)
        assert "-jsonl" in cmd

    def test_parse_output_text_fallback_still_works(self) -> None:
        """After migration: text fallback triggers when JSONL parsing fails."""
        tool = NucleiTool()
        text_output = "[critical] [CVE-2021-44228] http://example.com"
        args = NucleiArgs(target="http://example.com")
        metadata = tool.parse_output(text_output, "", 0, args)
        assert "results" in metadata

    def test_build_command_non_scan_mode_omits_jsonl(self) -> None:
        """Non-scan modes (update, list, etc.) must not force -jsonl."""
        tool = NucleiTool()
        for mode in [NucleiMode.UPDATE, NucleiMode.LIST, NucleiMode.VALIDATE]:
            args = NucleiArgs(target="http://example.com", mode=mode)
            cmd = tool.build_command(args)
            assert "-jsonl" not in cmd, f"-jsonl should not appear for mode={mode.value}"

    def test_build_command_scan_mode_includes_sarif_export_flag(self) -> None:
        """SARIF report export adds -se flag alongside internal -jsonl capture."""
        tool = NucleiTool()
        args = NucleiArgs(target="http://example.com", report_format=ReportFormat.SARIF)
        cmd = tool.build_command(args)
        assert "-jsonl" in cmd
        assert "-se" in cmd

    def test_build_command_scan_mode_includes_json_export_flag(self) -> None:
        """JSON report export adds -je flag alongside internal -jsonl capture."""
        tool = NucleiTool()
        args = NucleiArgs(target="http://example.com", report_format=ReportFormat.JSON)
        cmd = tool.build_command(args)
        assert "-jsonl" in cmd
        assert "-je" in cmd

    def test_report_format_rejected_for_non_scan_mode(self) -> None:
        """Report exports must not be accepted outside scan mode."""
        with pytest.raises(ValidationError):
            NucleiArgs(
                target="http://example.com",
                mode=NucleiMode.UPDATE,
                report_format=ReportFormat.JSON,
            )

    def test_adapter_extracts_from_metadata_results_key(self) -> None:
        adapter = NucleiKnowledgeAdapter()
        context = _adapter_context(
            tool_name="web_applications.web_vulnerability_scanners.nuclei",
            tool_metadata={
                "results": [
                    {
                        "template-id": "CVE-2021-44228",
                        "severity": "critical",
                        "matched-at": "http://example.com",
                    }
                ]
            },
        )
        observations = adapter.extract(context)
        findings = [o for o in observations if o.observation_type == "finding.vulnerability_detected"]
        assert len(findings) == 1
        assert "cve-2021-44228" in findings[0].subject_key


# ---------------------------------------------------------------------------
# SQLMap baseline
# ---------------------------------------------------------------------------


class TestSqlmapBaseline:
    """Freeze sqlmap metadata keys and artifact behavior."""

    def test_parse_output_json_returns_vulnerabilities_key(self) -> None:
        json_text = '{"data": [{"type": "boolean-based blind", "parameter": "id", "payload": "1 AND 1=1", "value": "true"}]}'
        metadata = parse_sqlmap_json(json_text)
        assert "vulnerabilities" in metadata
        assert len(metadata["vulnerabilities"]) == 1

    def test_parse_output_text_fallback_returns_vulnerabilities(self) -> None:
        text = "sqlmap identified the following injection point:\nParameter: id (GET)\n  Type: boolean-based blind vulnerable"
        metadata = parse_sqlmap_text(text)
        assert "vulnerabilities" in metadata
        assert len(metadata["vulnerabilities"]) >= 1

    def test_build_command_always_includes_json_output_format(self) -> None:
        tool = SqlmapTool()
        args = SqlmapArgs(target="http://example.com/page?id=1")
        cmd = tool.build_command(args)
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    def test_parse_output_text_fallback_still_works(self) -> None:
        """After migration: text fallback triggers when JSON parsing fails."""
        tool = SqlmapTool()
        text = "sqlmap identified sql injection vulnerable\n"
        args = SqlmapArgs(target="http://example.com/page?id=1")
        metadata = tool.parse_output(text, "", 0, args)
        assert "vulnerabilities" in metadata
        assert "stdout" in metadata

    def test_adapter_extracts_confirmed_findings_from_metadata(self) -> None:
        adapter = SqlmapKnowledgeAdapter()
        context = _adapter_context(
            tool_name="web_applications.web_vulnerability_scanners.sqlmap",
            tool_metadata={
                "vulnerabilities": [
                    {"parameter": "id", "type": "boolean-based blind"},
                ],
            },
            tool_arguments={"target": "http://example.com/vuln.php?id=1"},
        )
        observations = adapter.extract(context)
        confirmed = [o for o in observations if o.observation_type == "finding.vulnerability_confirmed"]
        assert len(confirmed) == 1


# ---------------------------------------------------------------------------
# Gobuster baseline
# ---------------------------------------------------------------------------


class TestGobusterBaseline:
    """Freeze gobuster metadata keys and artifact behavior."""

    def test_build_command_never_adds_json_flag(self) -> None:
        """Gobuster is text-native; build_command must never add -j."""
        tool = GobusterTool()
        args = GobusterArgs(target="http://example.com", wordlist="/usr/share/wordlists/common.txt", output_format="json")
        cmd = tool.build_command(args)
        assert "-j" not in cmd
        assert "--json" not in cmd

    def test_parse_output_returns_findings_and_found_paths_keys(self) -> None:
        tool = GobusterTool()
        stdout = "/admin (Status: 200) [Size: 1234]\n/login (Status: 301) [Size: 567]\n"
        args = GobusterArgs(target="http://example.com", wordlist="/usr/share/wordlists/common.txt")
        metadata = tool.parse_output(stdout, "", 0, args)
        assert "findings" in metadata
        assert "found_paths" in metadata
        assert "effective_output_format" in metadata
        assert metadata["effective_output_format"] == "text_parsed"

    def test_adapter_extracts_web_path_discovered(self) -> None:
        adapter = GobusterKnowledgeAdapter()
        context = _adapter_context(
            tool_name="web_applications.web_crawlers.gobuster",
            tool_metadata={
                "findings": [{"path": "/admin", "status": 200, "size": 1234}],
            },
            tool_arguments={"target": "http://example.com"},
        )
        observations = adapter.extract(context)
        path_obs = [o for o in observations if o.observation_type == "web.path_discovered"]
        assert len(path_obs) >= 1


# ---------------------------------------------------------------------------
# Msfconsole baseline
# ---------------------------------------------------------------------------


class TestMsfconsoleBaseline:
    """Freeze msfconsole metadata keys and artifact behavior."""

    def test_parse_output_returns_required_keys(self) -> None:
        output = """
[*] Started reverse TCP handler on 192.168.1.50:4444
[+] Meterpreter session 1 opened at 2024-01-15
"""
        metadata = parse_msfconsole_output(output)
        assert "success" in metadata
        assert "sessions" in metadata
        assert "modules_loaded" in metadata
        assert "errors" in metadata

    def test_tool_parse_output_returns_parsed_output_and_sessions_keys(self) -> None:
        tool = MsfRunExploitTool()
        output = "[+] Meterpreter session 1 opened\n"
        args = MsfRunExploitArgs(
            target="192.168.1.100",
            module_path="exploit/windows/smb/ms17_010_eternalblue",
        )
        metadata = tool.parse_output(output, "", 0, args)
        assert "parsed_output" in metadata
        assert "sessions_created" in metadata
        assert "modules_loaded" in metadata

    def test_adapter_extracts_exploit_succeeded(self) -> None:
        adapter = MsfconsoleKnowledgeAdapter()
        context = _adapter_context(
            tool_name="exploitation_tools.metasploit.run_exploit",
            tool_metadata={
                "parsed_output": {
                    "sessions": [{"id": "1", "type": "meterpreter"}],
                    "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
                    "raw_output": "[+] Meterpreter session 1 opened",
                },
                "sessions_created": 1,
                "modules_loaded": ["exploit/windows/smb/ms17_010_eternalblue"],
            },
            tool_arguments={
                "module_path": "exploit/windows/smb/ms17_010_eternalblue",
                "rhosts": "192.168.1.100",
                "lhost": "192.168.1.50",
            },
        )
        observations = adapter.extract(context)
        findings = [o for o in observations if o.observation_type == "finding.exploit_succeeded"]
        assert len(findings) == 1
        assert findings[0].payload.get("source") == "msfconsole"


# ---------------------------------------------------------------------------
# Silent metadata loss regression
# ---------------------------------------------------------------------------


class TestSilentMetadataLossRegression:
    """Regression: demonstrate that selecting a non-canonical output_format
    causes silent metadata loss for structured-native tools. These tests
    freeze the CURRENT broken behavior so we can prove the migration fixes it."""

    def test_nmap_canonical_capture_always_produces_metadata(self) -> None:
        """After migration: nmap always uses XML, so metadata is always present."""
        tool = NmapTool()
        args = NmapArgs(target="10.0.0.1")
        metadata = tool.parse_output(NMAP_XML, "", 0, args)
        assert len(metadata["open_ports"]) == 2
        assert metadata["hosts_up"] == 1

    def test_nmap_output_format_field_removed_from_schema(self) -> None:
        """After migration: output_format is no longer in the LLM-visible schema."""
        assert "output_format" not in NmapArgs.model_fields

    def test_masscan_canonical_capture_always_produces_metadata(self) -> None:
        """After migration: masscan always uses JSON, so metadata is always present."""
        tool = MasscanTool()
        from agent.tools.information_gathering.network_discovery.masscan import MasscanArgsV2
        args = MasscanArgsV2(target="10.0.0.1", ports="80")
        metadata = tool.parse_output(MASSCAN_JSON, "", 0, args)
        assert len(metadata["open_ports"]) == 1

    def test_masscan_output_format_field_removed_from_v2_schema(self) -> None:
        """After migration: output_format is no longer in the LLM-visible V2 schema."""
        from agent.tools.information_gathering.network_discovery.masscan import MasscanArgsV2
        assert "output_format" not in MasscanArgsV2.model_fields

    def test_nmap_adapter_produces_zero_observations_without_metadata_or_artifacts(self) -> None:
        """If nmap is run with non-xml format AND no useful artifacts,
        the adapter produces zero observations -- silent metadata loss."""
        adapter = NmapKnowledgeAdapter()
        context = _adapter_context(
            tool_name="information_gathering.network_discovery.nmap",
            tool_metadata={},
            artifacts=[],
        )
        observations = adapter.extract(context)
        assert observations == []

    def test_masscan_adapter_produces_zero_observations_without_metadata_or_artifacts(self) -> None:
        """If masscan is run with non-json format AND no useful artifacts,
        the adapter produces zero observations -- silent metadata loss."""
        adapter = MasscanKnowledgeAdapter()
        context = _adapter_context(
            tool_name="information_gathering.network_discovery.masscan",
            tool_metadata={},
            artifacts=[],
        )
        observations = adapter.extract(context)
        assert observations == []
