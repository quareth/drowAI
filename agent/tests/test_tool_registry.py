import subprocess
from types import SimpleNamespace

from agent.tools import (
    register_tool,
    tool_exists,
    get_tool,
    run_tool_by_name,
    available_tools,
    BaseTool,
    BaseToolArgs,
    ToolResult,
)


class DummyTool(BaseTool):
    args_model = BaseToolArgs

    def run(self, args: BaseToolArgs) -> ToolResult:
        return ToolResult(
            success=True,
            exit_code=0,
            stdout=args.target,
            stderr="",
            artifacts=[],
            metadata={},
            execution_time=0.0,
        )


def test_registry_registration_and_execution():
    register_tool("dummy", DummyTool)
    assert tool_exists("dummy")
    cls = get_tool("dummy")
    assert cls is DummyTool
    result = run_tool_by_name("dummy", {"target": "x"})
    assert result.stdout == "x"


def test_dynamic_discovery_and_execution(monkeypatch):
    name = "information_gathering.network_discovery.nmap"
    assert name in available_tools()
    cls = get_tool(name)
    assert cls.__name__ == "NmapTool"

    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, "<nmaprun/>", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_tool_by_name(name, {"target": "127.0.0.1"})
    assert result.exit_code == 0
    assert result.success is False
    assert result.metadata.get("warning") == "zero_hosts_scanned"


def test_http_tools_discoverable_in_registry():
    tools = set(available_tools())
    assert "information_gathering.web_enumeration.http_request" in tools
    assert "information_gathering.web_enumeration.http_download" in tools


def test_available_tools_excludes_helper_modules():
    tools = set(available_tools())

    assert "filesystem.aliases" not in tools
    assert "shell.policy" not in tools
    assert "web_applications.parsing_utils" not in tools
    assert "information_gathering.network_discovery.nmap_semantics" not in tools
    assert "web_applications.web_vulnerability_scanners.nuclei_semantics" not in tools


def test_available_tools_uses_explicit_class_tool_ids_for_multi_tool_modules():
    tools = set(available_tools())

    assert "filesystem.convenience" not in tools
    assert "filesystem.read_head" in tools
    assert "filesystem.read_tail" in tools
    assert "filesystem.grep" in tools
    assert get_tool("filesystem.read_head").__name__ == "FsReadHeadTool"
    assert get_tool("filesystem.read_tail").__name__ == "FsReadTailTool"
    assert get_tool("filesystem.grep").__name__ == "FsGrepTool"


def test_tool_catalog_entries_prefer_enhanced_metadata_description():
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries([
        "information_gathering.web_enumeration.http_request",
    ])

    description = entries[0]["description"]
    assert "Fetch one known HTTP(S) URL" in description
    assert "not for crawling or fuzzing" in description
    assert description != "Perform HTTP requests with structured output and secure defaults"
    assert len(description) <= 200


def test_tool_catalog_entries_filesystem_read_file_runbook_shape():
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries(["filesystem.read_file"])

    description = entries[0]["description"]
    assert "Read a workspace file without modifying it" in description
    assert "READ" not in description
    assert len(description) <= 200


def test_tool_catalog_entries_shell_exec_runbook_shape():
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries(["shell.exec"])

    description = entries[0]["description"]
    assert "Execute one guarded shell command" in description
    assert "stdout" in description
    assert len(description) <= 200


def test_tool_catalog_entries_hydra_runbook_shape():
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries(["password_attacks.online_attacks.hydra"])

    description = entries[0]["description"]
    assert "Brute-force or credential-spray network logins" in description
    assert "SSH" in description
    assert len(description) <= 200


def test_tool_catalog_entries_network_utility_runbook_shape():
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries(["networking_utilities.network"])

    description = entries[0]["description"]
    assert "Run finite network utility checks" in description
    assert "whois" in description
    assert "not for scanning or HTTP" in description
    assert len(description) <= 200


def test_tool_catalog_entries_service_access_category_and_description():
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries(["service_access.ftp_login"])

    assert entries[0]["category"] == "service_access"
    description = entries[0]["description"]
    assert "Authenticate to one FTP service" in description
    assert len(description) <= 200


def test_tool_catalog_entries_tcpdump_runbook_shape():
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    entries = build_tool_catalog_entries(["sniffing_spoofing.network_sniffers.tcpdump"])

    description = entries[0]["description"]
    assert "Capture network packets" in description
    assert "passive" in description
    assert "Captures and inspects" not in description
    assert len(description) <= 200


def test_tool_catalog_entries_hides_tools_removed_from_llm_catalogs():
    from agent.tools.catalog_visibility import is_tool_hidden_from_catalog

    for tool_id in [
        "shell.exec",
        "exploitation_tools.metasploit.run_exploit",
        "information_gathering.osint.whois",
        "information_gathering.network_discovery.netdiscover",
        "reverse_engineering.debuggers.immunity_debugger",
        "reverse_engineering.debuggers.edb",
        "reverse_engineering.debuggers.ollydbg",
        "reverse_engineering.hex_editors.hexedit",
        "reverse_engineering.hex_editors.bless",
    ]:
        assert is_tool_hidden_from_catalog(tool_id), tool_id

    assert not is_tool_hidden_from_catalog("filesystem.grep")


def test_available_tools_excludes_netdiscover_from_catalog():
    from agent.tools.tool_registry import available_tools

    assert "information_gathering.network_discovery.netdiscover" not in available_tools()


def test_tool_catalog_entries_limit_descriptions_to_200_chars(monkeypatch):
    from agent.tools import enhanced_metadata_registry
    from agent.tools.enhanced_tool_metadata import build_tool_catalog_entries

    long_description = "x" * 260
    monkeypatch.setattr(
        enhanced_metadata_registry,
        "get_enhanced_tool_metadata",
        lambda _tool_id: SimpleNamespace(
            capabilities=[SimpleNamespace(description=long_description)]
        ),
    )
    monkeypatch.setattr(
        "agent.tools.tool_registry.get_tool_metadata",
        lambda _tool_id: {"name": "example.tool", "description": "fallback"},
    )

    entries = build_tool_catalog_entries(["example.tool"])

    assert len(entries[0]["description"]) == 200
    assert entries[0]["description"].endswith("...")
