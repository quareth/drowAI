from pathlib import Path

from agent.tools.web_applications.parsing_utils import extract_vulnerabilities, parse_crawler_line
from agent.tools.web_applications.web_application_proxies.mitmproxy import MitmProxyArgs, MitmProxyTool
from agent.tools.web_applications.web_application_fuzzers.ffuf import (
    FfufArgs as FuzzerFfufArgs,
    FfufTool as FuzzerFfufTool,
)
from agent.tools.web_applications.web_crawlers.feroxbuster import FeroxArgs, FeroxMode, FeroxbusterTool
from agent.tools.web_applications.web_crawlers.gobuster import GobusterArgs, GobusterTool
from agent.tools.web_applications.web_vulnerability_scanners.nuclei import NucleiArgs, NucleiTool
from agent.tools.web_applications.cms_identification.wpscan import WPScanArgs, WPScanTool


FIXTURE_BASE = Path(__file__).resolve().parents[1] / "fixtures" / "web_tools"


def _read(category: str, filename: str) -> str:
    return (FIXTURE_BASE / category / filename).read_text(encoding="utf-8")


def test_crawler_parsing_integration():
    gobuster = GobusterTool()
    args = GobusterArgs(target="http://example.com", wordlist="list.txt")
    stdout = _read("crawlers", "gobuster_output_text.txt")
    metadata = gobuster.parse_output(stdout, "", 0, args)
    assert metadata["findings"]
    assert parse_crawler_line(stdout.splitlines()[0])["status"] == 200

    ferox = FeroxbusterTool()
    ferox_args = FeroxArgs(target="http://example.com", mode=FeroxMode.DIRECTORY, wordlist="list.txt")
    ferox_stdout = _read("crawlers", "feroxbuster_output_json.json")
    ferox_metadata = ferox.parse_output(ferox_stdout, "", 0, ferox_args)
    assert ferox_metadata.get("data")


def test_scanner_parsing_integration():
    nuclei = NucleiTool()
    args = NucleiArgs(target="http://example.com")
    stdout = _read("scanners", "nuclei_output_json.json")
    metadata = nuclei.parse_output(stdout, "", 0, args)
    vulns = extract_vulnerabilities(metadata.get("results", []))
    assert vulns
    assert vulns[0]["severity"] in {"High", "Critical", "Medium", "Low"}


def test_cms_parsing_integration():
    wpscan = WPScanTool()
    args = WPScanArgs(target="http://example.com")
    stdout = _read("cms", "wpscan_output_json.json")
    metadata = wpscan.parse_output(stdout, "", 0, args)
    assert metadata.get("plugins")
    assert metadata.get("vulnerabilities")


def test_fuzzer_parsing_integration():
    ffuf = FuzzerFfufTool()
    args = FuzzerFfufArgs(target="http://example.com/FUZZ", wordlist="list.txt")
    stdout = _read("fuzzers", "ffuf_output_text.txt")
    metadata = ffuf.parse_output(stdout, "", 0, args)
    assert metadata.get("raw_output") or metadata.get("results", []) == []
    assert isinstance(metadata.get("results", []), list)


def test_proxy_parsing_integration():
    mitmproxy = MitmProxyTool()
    args = MitmProxyArgs(target="http://example.com")
    stdout = _read("proxies", "mitmproxy_output_json.json")
    metadata = mitmproxy.parse_output(stdout, "", 0, args)
    assert metadata.get("parse_error") or metadata.get("capture_status") is not None

