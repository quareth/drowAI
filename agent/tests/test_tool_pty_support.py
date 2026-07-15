"""Tests for tool PTY support methods (build_command, parse_output, create_artifacts).

 of PTY Execution Implementation Plan."""

import pytest
import os
import tempfile
from unittest.mock import patch
from pydantic import ValidationError

# Nmap tool tests
from agent.tools.information_gathering.network_discovery.nmap import (
    NmapTool, NmapArgs, ScanType, TimingTemplate, parse_nmap_xml
)

# Masscan tool tests
from agent.tools.information_gathering.network_discovery.masscan import (
    MasscanTool,
    MasscanArgs,
    HostDiscoveryMode,
    MasscanCapabilities,
    parse_masscan_json,
)

# Amass tool tests
from agent.tools.information_gathering.dns.amass import (
    AmassTool, AmassArgs, Mode as AmassMode, OutputFormat as AmassOutputFormat, parse_amass_json
)

# TheHarvester tool tests
from agent.tools.information_gathering.osint.theharvester import (
    TheHarvesterTool, TheHarvesterArgs, SearchEngine, OutputFormat as HarvesterOutputFormat,
    parse_theharvester_json
)

# Traceroute tool tests
from agent.tools.information_gathering.route_analysis.traceroute import (
    TracerouteTool, TracerouteArgs
)
from agent.tools.information_gathering.web_enumeration.http_download import HttpDownloadTool
from agent.tools.information_gathering.web_enumeration.http_request import HttpRequestTool
from agent.tools.information_gathering.web_enumeration.contracts import HttpDownloadArgs, HttpRequestArgs


class TestNmapBuildCommand:
    """Test NmapTool.build_command() generates correct commands."""
    
    def test_basic_syn_scan(self):
        """Test basic SYN scan command generation."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80,443")
        cmd = tool.build_command(args)
        
        assert cmd[0] == "nmap"
        assert "-T4" in cmd  # Default timing
        assert "-sS" in cmd  # Default SYN scan
        assert "-p" in cmd
        idx = cmd.index("-p")
        assert cmd[idx + 1] == "80,443"
        assert "192.168.1.1" in cmd
    
    def test_host_discovery_scan(self):
        """Test host discovery (ping sweep) command."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.0/24", scan_types=[ScanType.HOST_DISCOVERY])
        cmd = tool.build_command(args)
        
        assert "nmap" in cmd
        assert "-sn" in cmd
        assert "192.168.1.0/24" in cmd
    
    def test_service_detection(self):
        """Test service detection flag."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80", service_detection=True)
        cmd = tool.build_command(args)
        
        assert "-sV" in cmd
    
    def test_os_detection(self):
        """Test OS detection flag."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80", os_detection=True)
        cmd = tool.build_command(args)
        
        assert "-O" in cmd
    
    def test_aggressive_scan(self):
        """Test aggressive scan flag."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80", aggressive=True)
        cmd = tool.build_command(args)
        
        assert "-A" in cmd
    
    def test_skip_host_discovery(self):
        """Test skip host discovery flag."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80", skip_host_discovery=True)
        cmd = tool.build_command(args)
        
        assert "-Pn" in cmd
    
    def test_disable_dns(self):
        """Test disable DNS flag."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80", disable_dns=True)
        cmd = tool.build_command(args)
        
        assert "-n" in cmd
    
    def test_timing_template(self):
        """Test different timing templates."""
        tool = NmapTool()
        
        for timing in TimingTemplate:
            args = NmapArgs(target="192.168.1.1", ports="80", timing=timing)
            cmd = tool.build_command(args)
            assert timing.value in cmd
    
    def test_canonical_xml_output_always_present(self):
        """Test XML output is always used (canonical capture)."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80")
        cmd = tool.build_command(args)

        assert "-oX" in cmd
        idx = cmd.index("-oX")
        assert cmd[idx + 1] == "-"
    
    def test_multiple_targets(self):
        """Test multiple targets separated by comma/space."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1, 192.168.1.2", ports="80")
        cmd = tool.build_command(args)
        
        assert "192.168.1.1" in cmd
        assert "192.168.1.2" in cmd
    
    def test_rate_limiting(self):
        """Test rate limiting options."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80", max_rate=1000, min_rate=100)
        cmd = tool.build_command(args)
        
        assert "--max-rate" in cmd
        assert "1000" in cmd
        assert "--min-rate" in cmd
        assert "100" in cmd
    
    def test_scripts(self):
        """Test NSE script execution."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1", ports="80", scripts=["http-title", "http-headers"])
        cmd = tool.build_command(args)
        
        assert "--script" in cmd
        idx = cmd.index("--script")
        assert "http-title,http-headers" in cmd[idx + 1]
    
    def test_supports_pty(self):
        """Test that NmapTool reports PTY support."""
        tool = NmapTool()
        assert tool.supports_pty() is True


class TestNmapParseOutput:
    """Test NmapTool.parse_output() handles various outputs."""
    
    def test_xml_parsing_with_open_ports(self):
        """Test parsing XML with open ports."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1")

        xml_output = """<?xml version="1.0"?>
<nmaprun>
    <host>
        <address addr="192.168.1.1" addrtype="ipv4"/>
        <status state="up"/>
        <ports>
            <port portid="80" protocol="tcp">
                <state state="open"/>
                <service name="http"/>
            </port>
            <port portid="443" protocol="tcp">
                <state state="open"/>
                <service name="https"/>
            </port>
        </ports>
    </host>
    <runstats>
        <hosts up="1" down="0" total="1"/>
    </runstats>
</nmaprun>"""

        metadata = tool.parse_output(xml_output, "", 0, args)

        assert metadata["hosts_up"] == 1
        assert metadata["hosts_total"] == 1
        assert len(metadata["open_ports"]) == 2
        assert any(p["port"] == 80 for p in metadata["open_ports"])
        assert any(p["port"] == 443 for p in metadata["open_ports"])

    def test_xml_parsing_host_down(self):
        """Test parsing XML with host down."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1")

        xml_output = """<?xml version="1.0"?>
<nmaprun>
    <runstats>
        <hosts up="0" down="1" total="1"/>
    </runstats>
</nmaprun>"""

        metadata = tool.parse_output(xml_output, "", 0, args)

        assert metadata["hosts_up"] == 0
        assert metadata["hosts_total"] == 1
        assert metadata["hosts_down"] == 1

    def test_invalid_xml_returns_error(self):
        """Test invalid XML returns error in metadata."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1")
        
        metadata = tool.parse_output("not valid xml", "", 0, args)
        
        assert "error" in metadata


class TestNmapCreateArtifacts:
    """Test NmapTool.create_artifacts() creates files correctly."""
    
    def test_creates_xml_artifact(self):
        """Test XML artifact file creation (canonical capture always XML)."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1")

        xml_output = "<nmaprun>test</nmaprun>"

        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                artifacts = tool.create_artifacts(xml_output, args, timestamp=12345)

                assert len(artifacts) == 1
                assert "nmap_12345.xml" in artifacts[0]
                assert os.path.exists(artifacts[0])

                with open(artifacts[0]) as f:
                    assert f.read() == xml_output
            finally:
                os.chdir(original_cwd)

    def test_no_artifact_for_empty_output(self):
        """Test no artifact created when output is empty."""
        tool = NmapTool()
        args = NmapArgs(target="192.168.1.1")

        artifacts = tool.create_artifacts("", args)

        assert artifacts == []


class TestMasscanBuildCommand:
    """Test MasscanTool.build_command() generates correct commands."""
    
    def test_basic_scan(self):
        """Test basic masscan command generation."""
        tool = MasscanTool()
        args = MasscanArgs(target="192.168.1.0/24", ports="80,443")
        cmd = tool.build_command(args)
        
        assert cmd[0] == "masscan"
        assert "-p" in cmd
        assert "192.168.1.0/24" in cmd
    
    def test_rate_options(self):
        """Test integer rate options."""
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        tool = MasscanTool()

        for rate in (1000, 10000, 1000000):
            args = MasscanArgs(target="192.168.1.1", ports="80", rate=rate)
            cmd = tool.build_command(args)
            idx = cmd.index("--rate")
            assert cmd[idx + 1] == str(rate)
    
    def test_canonical_json_output_always_present(self):
        """Test JSON output is always used (canonical capture)."""
        tool = MasscanTool()
        args = MasscanArgs(target="192.168.1.1", ports="80")
        cmd = tool.build_command(args)

        assert "-oJ" in cmd
    
    def test_banner_grabbing(self):
        """Test banner grabbing option."""
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        tool = MasscanTool()
        args = MasscanArgs(target="192.168.1.1", ports="80", banners=True)
        cmd = tool.build_command(args)
        
        assert "--banners" in cmd

    def test_host_discovery_no_ping(self):
        """Test no-ping host discovery mode."""
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        tool = MasscanTool()
        args = MasscanArgs(
            target="192.168.1.1",
            ports="80",
            host_discovery=HostDiscoveryMode.NO_PING,
        )
        cmd = tool.build_command(args)
        assert "--no-ping" in cmd

    def test_runtime_flag_fallback_for_retries(self, monkeypatch):
        """Use --max-retries when --retries is unavailable."""
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        caps = MasscanCapabilities(flags=frozenset({"--max-retries"}))
        monkeypatch.setattr(
            "agent.tools.information_gathering.network_discovery.masscan.detect_masscan_capabilities",
            lambda: caps,
        )

        tool = MasscanTool()
        args = MasscanArgs(target="192.168.1.1", ports="80", retries=2)
        cmd = tool.build_command(args)

        assert "--max-retries" in cmd
        assert "--retries" not in cmd

    def test_runtime_flag_fail_fast_for_unsupported_semantic(self, monkeypatch):
        """Fail fast when requested semantic has no supported runtime flag."""
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        caps = MasscanCapabilities(flags=frozenset({"--rate"}))
        monkeypatch.setattr(
            "agent.tools.information_gathering.network_discovery.masscan.detect_masscan_capabilities",
            lambda: caps,
        )

        tool = MasscanTool()
        args = MasscanArgs(target="192.168.1.1", ports="80", retries=1)
        with pytest.raises(ValueError):
            tool.build_command(args)

    def test_canonical_flags_do_not_emit_legacy_names(self, monkeypatch):
        """Canonical params should not emit legacy flag names."""
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        caps = MasscanCapabilities(
            flags=frozenset(
                {
                    "--retries",
                    "-e",
                    "--adapter-ip",
                    "-iL",
                    "--excludefile",
                    "--no-ping",
                    "--open-only",
                    "--banners",
                }
            )
        )
        monkeypatch.setattr(
            "agent.tools.information_gathering.network_discovery.masscan.detect_masscan_capabilities",
            lambda: caps,
        )

        tool = MasscanTool()
        args = MasscanArgs(
            target="192.168.1.0/24",
            ports="1-100",
            retries=1,
            adapter="eth0",
            adapter_ip="192.168.1.10",
            include_file="targets.txt",
            exclude_file="exclude.txt",
            host_discovery=HostDiscoveryMode.NO_PING,
            open_only=True,
            banners=True,
        )
        cmd = tool.build_command(args)

        assert "--retries" in cmd
        assert "-e" in cmd
        assert "--adapter-ip" in cmd
        assert "-iL" in cmd
        assert "--excludefile" in cmd

        assert "--max-retries" not in cmd
        assert "--source-ip" not in cmd
        assert "--include-file" not in cmd
        assert "--exclude-file" not in cmd
        assert "-i" not in cmd
    
    def test_supports_pty(self):
        """Test that MasscanTool reports PTY support."""
        tool = MasscanTool()
        assert tool.supports_pty() is True


class TestMasscanParseOutput:
    """Test MasscanTool.parse_output() handles various outputs."""
    
    def test_json_parsing(self):
        """Test parsing JSON output (canonical capture always JSON)."""
        tool = MasscanTool()
        args = MasscanArgs(target="192.168.1.1")

        json_output = '{"ip": "192.168.1.1", "ports": [{"port": 80, "proto": "tcp", "status": "open"}]}'

        metadata = tool.parse_output(json_output, "", 0, args)

        assert len(metadata["open_ports"]) == 1
        assert metadata["open_ports"][0]["port"] == 80


class TestMasscanSchemaValidation:
    """Masscan schema validation and alias compatibility checks."""

    def test_rejects_unknown_parameters(self):
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        with pytest.raises(ValidationError):
            MasscanArgs(target="192.168.1.1", unknown_param=True)

    def test_alias_fields_are_accepted_and_mapped(self):
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        args = MasscanArgs(
            target="192.168.1.1",
            interface="eth0",
            source_ip="192.168.1.10",
            max_retries=0,
            banner=True,
            ping=False,
        )

        assert args.adapter == "eth0"
        assert args.adapter_ip == "192.168.1.10"
        assert args.retries == 0
        assert args.banners is True
        assert args.host_discovery == HostDiscoveryMode.NO_PING
        assert len(args.deprecations) >= 1

    def test_invalid_ports_are_rejected(self):
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        with pytest.raises(ValidationError):
            MasscanArgs(target="192.168.1.1", ports="top-ports 100")

    def test_target_rejects_domain_hostname(self):
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        with pytest.raises(ValidationError):
            MasscanArgs(target="example.com", ports="80")

    def test_target_accepts_and_normalizes_multiple_ip_forms(self):
        if "host_discovery" not in MasscanArgs.model_fields:
            pytest.skip("MASSCAN_SCHEMA_V2 disabled")

        args = MasscanArgs(
            target="192.168.1.10 192.168.1.20-192.168.1.30,192.168.1.0/24",
            ports="80",
        )
        assert args.target == "192.168.1.10,192.168.1.20-192.168.1.30,192.168.1.0/24"


class TestAmassBuildCommand:
    """Test AmassTool.build_command() generates correct commands."""
    
    def test_passive_enum(self):
        """Test passive enumeration command."""
        tool = AmassTool()
        args = AmassArgs(target="example.com", mode=AmassMode.PASSIVE)
        cmd = tool.build_command(args)
        
        assert cmd[0] == "amass"
        assert "enum" in cmd
        assert "-passive" in cmd
        assert "example.com" in cmd
    
    def test_active_enum(self):
        """Test active enumeration command."""
        tool = AmassTool()
        args = AmassArgs(target="example.com", mode=AmassMode.ACTIVE)
        cmd = tool.build_command(args)
        
        assert "enum" in cmd
        assert "-passive" not in cmd
    
    def test_brute_mode(self):
        """Test brute force mode."""
        tool = AmassTool()
        args = AmassArgs(target="example.com", mode=AmassMode.BRUTE)
        cmd = tool.build_command(args)
        
        assert "-brute" in cmd
    
    def test_supports_pty(self):
        """Test that AmassTool reports PTY support."""
        tool = AmassTool()
        assert tool.supports_pty() is True


class TestTheHarvesterBuildCommand:
    """Test TheHarvesterTool.build_command() generates correct commands."""
    
    def test_basic_search(self):
        """Test basic search command."""
        tool = TheHarvesterTool()
        args = TheHarvesterArgs(target="example.com")
        cmd = tool.build_command(args)
        
        assert cmd[0] == "theHarvester"
        assert "-b" in cmd
        assert "example.com" in cmd
    
    def test_search_engines(self):
        """Test search engine selection."""
        tool = TheHarvesterTool()
        args = TheHarvesterArgs(
            target="example.com",
            search_engines=[SearchEngine.GOOGLE, SearchEngine.SHODAN]
        )
        cmd = tool.build_command(args)
        
        idx = cmd.index("-b")
        assert "google,shodan" in cmd[idx + 1]
    
    def test_limit_option(self):
        """Test result limit option."""
        tool = TheHarvesterTool()
        args = TheHarvesterArgs(target="example.com", limit=100)
        cmd = tool.build_command(args)
        
        assert "-l" in cmd
        idx = cmd.index("-l")
        assert cmd[idx + 1] == "100"
    
    def test_supports_pty(self):
        """Test that TheHarvesterTool reports PTY support."""
        tool = TheHarvesterTool()
        assert tool.supports_pty() is True


class TestBaseToolSupportsPtr:
    """Test BaseTool.supports_pty() detection logic."""
    
    def test_tool_without_build_command_not_supported(self):
        """Test that tools without build_command() override return False."""
        from agent.tools.base_tool import BaseTool
        from agent.tools.schemas import ToolResult
        from pydantic import BaseModel
        
        class DummyArgs(BaseModel):
            target: str
        
        class DummyTool(BaseTool):
            args_model = DummyArgs
            
            def run(self, args):
                return ToolResult(
                    success=True, exit_code=0, stdout="", stderr="",
                    artifacts=[], metadata={}, execution_time=0.0
                )
        
        tool = DummyTool()
        assert tool.supports_pty() is False
    
    def test_tool_with_build_command_supported(self):
        """Test that tools with build_command() override return True."""
        from agent.tools.base_tool import BaseTool
        from agent.tools.schemas import ToolResult
        from pydantic import BaseModel
        from typing import List
        
        class DummyArgs(BaseModel):
            target: str
        
        class PTYEnabledTool(BaseTool):
            args_model = DummyArgs
            
            def build_command(self, args) -> List[str]:
                return ["echo", args.target]
            
            def run(self, args):
                return ToolResult(
                    success=True, exit_code=0, stdout="", stderr="",
                    artifacts=[], metadata={}, execution_time=0.0
                )
        
        tool = PTYEnabledTool()
        assert tool.supports_pty() is True


def test_traceroute_supports_pty_and_builds_minimal_command() -> None:
    tool = TracerouteTool()
    assert tool.supports_pty() is True

    args = TracerouteArgs(target="172.17.0.1")
    cmd = tool.build_command(args)
    assert cmd == ["traceroute", "172.17.0.1"]


def test_http_request_supports_pty_and_builds_command() -> None:
    tool = HttpRequestTool()
    assert tool.supports_pty() is True

    args = HttpRequestArgs(target="https://example.com", method="HEAD")
    cmd = tool.build_command(args)
    assert cmd[0] == "curl"
    assert "--head" in cmd
    assert "HEAD" not in cmd
    assert cmd[-1] == "https://example.com"


def test_http_download_supports_pty_and_uses_relative_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    tool = HttpDownloadTool()
    assert tool.supports_pty() is True

    args = HttpDownloadArgs(target="https://example.com/file.bin", output_path="downloads/file.bin")
    cmd = tool.build_command(args)
    assert cmd[0] == "curl"
    assert "--output" in cmd
    output_idx = cmd.index("--output") + 1
    assert cmd[output_idx] == "downloads/file.bin"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
