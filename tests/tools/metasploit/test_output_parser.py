"""
Tests for the Metasploit Output Parser.

The output parser extracts structured data from raw msfconsole output.
"""

from __future__ import annotations

import pytest

from agent.tools.exploitation_tools.metasploit.output_parser import (
    SessionInfo,
    JobInfo,
    ModuleInfo,
    MsfParseResult,
    parse_msfconsole_output,
    parse_search_results,
    parse_session_list,
    parse_module_info,
    to_dict,
)


class TestParseMsfconsoleOutput:
    """Test main parsing function."""

    def test_parse_session_creation(self, sample_session_output):
        """Parse session creation from output."""
        result = parse_msfconsole_output(sample_session_output)

        assert result.success is True
        assert len(result.sessions) == 1
        assert result.sessions[0].id == 1
        assert result.sessions[0].type == "meterpreter"

    def test_parse_exploit_completion(self, sample_exploit_output):
        """Parse exploit completion output."""
        result = parse_msfconsole_output(sample_exploit_output)

        assert result.success is True
        # Exploit completion is detected via success pattern even without explicit session message

    def test_parse_auxiliary_scan(self, sample_auxiliary_output):
        """Parse auxiliary scan output."""
        result = parse_msfconsole_output(sample_auxiliary_output)

        assert result.success is True
        # Should detect vulnerability status
        assert result.extra.get("target_vulnerable") is True

    def test_parse_error_output(self, sample_error_output):
        """Parse error messages from output."""
        result = parse_msfconsole_output(sample_error_output)

        assert result.success is False
        assert len(result.errors) >= 1

    def test_parse_job_creation(self, sample_job_output):
        """Parse background job creation."""
        result = parse_msfconsole_output(sample_job_output)

        assert len(result.jobs) >= 1
        assert result.jobs[0].id == 1

    def test_parse_search_results(self, sample_search_output):
        """Parse search results."""
        result = parse_msfconsole_output(sample_search_output)

        assert "search_results_count" in result.extra
        assert result.extra["search_results_count"] >= 1

    def test_parse_empty_output(self):
        """Handle empty output gracefully."""
        result = parse_msfconsole_output("")

        assert result.success is False
        assert len(result.sessions) == 0
        assert len(result.jobs) == 0


class TestParseSearchResults:
    """Test search result parsing."""

    def test_parse_search_results_structure(self, sample_search_output):
        """Parse search results into structured format."""
        results = parse_search_results(sample_search_output)

        assert len(results) >= 1
        assert "index" in results[0]
        assert "type" in results[0]
        assert "path" in results[0]
        assert "name" in results[0]

    def test_parse_search_results_module_types(self, sample_search_output):
        """Verify module types are extracted."""
        results = parse_search_results(sample_search_output)

        types = {r["type"] for r in results}
        assert "exploit" in types or "auxiliary" in types


class TestParseSessionList:
    """Test session list parsing."""

    def test_parse_session_list(self):
        """Parse sessions -l output."""
        output = """
Active sessions
===============

  Id  Name  Type                     Information                     Connection
  --  ----  ----                     -----------                     ----------
  1         meterpreter x86/windows  DESKTOP-ABC123\\Admin @ ADMIN  192.168.1.100:4444 -> 192.168.1.50:49158
  2         shell                    Command shell                   192.168.1.100:4445 -> 192.168.1.51:49200
"""
        sessions = parse_session_list(output)

        assert len(sessions) == 2
        assert sessions[0].id == 1
        assert sessions[0].type == "meterpreter"
        assert sessions[1].id == 2
        assert sessions[1].type == "shell"


class TestParseModuleInfo:
    """Test module info parsing."""

    def test_parse_module_info(self):
        """Parse module info output."""
        output = """
       Name: MS17-010 EternalBlue SMB Remote Windows Kernel Pool Corruption
     Module: exploit/windows/smb/ms17_010_eternalblue
   Platform: Windows
       Arch: x64
 Privileged: Yes
    License: Metasploit Framework License (BSD)
       Rank: Average

Provided by:
  Sean Dillon <sean.dillon@risksense.com>
  Dylan Davis <dylan.davis@risksense.com>
  Equation Group

Module side effects:
 ioc-in-logs
 artifacts-on-disk

References:
  https://docs.microsoft.com/en-us/security-updates/SecurityBulletins/2017/MS17-010
  https://nvd.nist.gov/vuln/detail/CVE-2017-0143

Description:
  This module exploits a vulnerability in the Windows SMB service
  known as EternalBlue.
"""
        info = parse_module_info(output)

        assert info.get("name") == "MS17-010 EternalBlue SMB Remote Windows Kernel Pool Corruption"
        assert info.get("rank") == "Average"
        assert "authors" in info or "references" in info


class TestMsfParseResult:
    """Test MsfParseResult dataclass."""

    def test_result_to_dict(self):
        """Test converting result to dictionary."""
        result = MsfParseResult(
            success=True,
            sessions=[SessionInfo(id=1, type="meterpreter")],
            jobs=[JobInfo(id=1, name="handler")],
            modules=[ModuleInfo(path="exploit/multi/handler")],
            errors=[],
            warnings=["Test warning"],
            info=["Test info"],
            raw_output="test output",
        )

        d = to_dict(result)

        assert d["success"] is True
        assert len(d["sessions"]) == 1
        assert d["sessions"][0]["id"] == 1
        assert len(d["jobs"]) == 1
        assert len(d["modules"]) == 1


class TestSessionInfo:
    """Test SessionInfo dataclass."""

    def test_session_info_defaults(self):
        """Test SessionInfo default values."""
        session = SessionInfo(id=1)

        assert session.id == 1
        assert session.type == "shell"
        assert session.target_host is None
        assert session.opened is True


class TestJobInfo:
    """Test JobInfo dataclass."""

    def test_job_info_defaults(self):
        """Test JobInfo default values."""
        job = JobInfo(id=1)

        assert job.id == 1
        assert job.name == ""
        assert job.module == ""


class TestModuleInfo:
    """Test ModuleInfo dataclass."""

    def test_module_info_creation(self):
        """Test ModuleInfo creation."""
        module = ModuleInfo(
            path="exploit/windows/smb/ms17_010_eternalblue",
            type="exploit",
            rank="average",
        )

        assert module.path == "exploit/windows/smb/ms17_010_eternalblue"
        assert module.type == "exploit"
