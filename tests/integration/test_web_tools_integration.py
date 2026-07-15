import subprocess
from typing import Any, Dict, List, Tuple

import pytest

from agent.tests.tools.web_applications.conftest import assert_execution_model_compliance
from agent.tools.web_applications.cms_identification.cmsmap import CMSmapArgs, CMSmapTool
from agent.tools.web_applications.cms_identification.droopescan import (
    DroopescanArgs,
    DroopescanTool,
)
from agent.tools.web_applications.cms_identification.joomscan import (
    JoomScanArgs,
    JoomScanTool,
)
from agent.tools.web_applications.cms_identification.whatweb import WhatWebArgs, WhatWebTool
from agent.tools.web_applications.cms_identification.wpscan import WPScanArgs, WPScanTool
from agent.tools.web_applications.web_application_fuzzers.ffuf import (
    FfufArgs as FuzzerFfufArgs,
)
from agent.tools.web_applications.web_application_fuzzers.ffuf import (
    FfufTool as FuzzerFfufTool,
)
from agent.tools.web_applications.web_application_fuzzers.wfuzz import (
    WfuzzArgs as FuzzerWfuzzArgs,
)
from agent.tools.web_applications.web_application_fuzzers.wfuzz import (
    WfuzzTool as FuzzerWfuzzTool,
)
from agent.tools.web_applications.web_application_proxies.mitmproxy import (
    MitmProxyArgs,
    MitmProxyTool,
)
from agent.tools.web_applications.web_crawlers.dirb import DirbArgs, DirbTool
from agent.tools.web_applications.web_crawlers.feroxbuster import (
    FeroxArgs,
    FeroxMode,
    FeroxbusterTool,
)
from agent.tools.web_applications.web_crawlers.ffuf import (
    FfufArgs as CrawlerFfufArgs,
)
from agent.tools.web_applications.web_crawlers.ffuf import (
    FfufTool as CrawlerFfufTool,
)
from agent.tools.web_applications.web_crawlers.gobuster import GobusterArgs, GobusterTool
from agent.tools.web_applications.web_crawlers.wfuzz import (
    WfuzzArgs as CrawlerWfuzzArgs,
)
from agent.tools.web_applications.web_crawlers.wfuzz import (
    WfuzzTool as CrawlerWfuzzTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.commix import (
    CommixArgs,
    CommixTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.nikto import (
    NiktoArgs,
    NiktoTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.nuclei import (
    NucleiArgs,
    NucleiTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.skipfish import (
    SkipfishArgs,
    SkipfishTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.sqlmap import (
    SqlmapArgs,
    SqlmapTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.wapiti import (
    WapitiArgs,
    WapitiTool,
)
from agent.tools.web_applications.web_vulnerability_scanners.xsser import (
    XsserArgs,
    XsserTool,
)


ToolCase = Tuple[Any, Any, Dict[str, Any], str]


def _tool_cases() -> List[ToolCase]:
    """Aggregate minimal args and sample stdout per tool for compliance checks."""
    return [
        # Crawlers
        (GobusterTool, GobusterArgs, {"target": "http://example.com", "wordlist": "list.txt"}, "/admin (Status: 200)"),
        (DirbTool, DirbArgs, {"target": "http://example.com", "wordlist": "list.txt"}, "+ http://example.com/admin (CODE:200|SIZE:123)"),
        (FeroxbusterTool, FeroxArgs, {"target": "http://example.com", "mode": FeroxMode.DIRECTORY, "wordlist": "list.txt"}, '{"url":"http://example.com/admin","status":200}'),
        (CrawlerFfufTool, CrawlerFfufArgs, {"target": "http://example.com/FUZZ", "wordlist": "list.txt"}, "http://example.com/admin 200 123 10"),
        (CrawlerWfuzzTool, CrawlerWfuzzArgs, {"target": "http://example.com", "wordlist": "list.txt"}, "00001 200 123 10 1"),
        # Scanners
        (SqlmapTool, SqlmapArgs, {"target": "http://example.com"}, '{"data": [{"type": "sqli"}]}'),
        (NiktoTool, NiktoArgs, {"target": "http://example.com"}, '{"results": [{"risk": "3"}]}'),
        (WapitiTool, WapitiArgs, {"target": "http://example.com"}, '{"vulnerabilities": [{"name": "xss"}]}'),
        (NucleiTool, NucleiArgs, {"target": "http://example.com"}, '[{"template":"cve-2024-0001","severity":"high"}]'),
        (SkipfishTool, SkipfishArgs, {"target": "http://example.com"}, '{"vulnerabilities": [{"type": "xss"}]}'),
        (CommixTool, CommixArgs, {"target": "http://example.com", "injection_method": "get"}, "Vulnerable parameter: id"),
        (XsserTool, XsserArgs, {"target": "http://example.com", "payload": "<script>alert(1)</script>"}, "XSS found"),
        # CMS
        (WPScanTool, WPScanArgs, {"target": "http://example.com"}, '{"version": "6.5"}'),
        (WhatWebTool, WhatWebArgs, {"target": "http://example.com"}, '{"plugins": []}'),
        (JoomScanTool, JoomScanArgs, {"target": "http://example.com"}, "Joomla found"),
        (DroopescanTool, DroopescanArgs, {"target": "http://example.com"}, '{"results": []}'),
        (CMSmapTool, CMSmapArgs, {"target": "http://example.com"}, '{"vulnerabilities": []}'),
        # Fuzzers
        (FuzzerFfufTool, FuzzerFfufArgs, {"target": "http://example.com/FUZZ", "wordlist": "list.txt"}, "http://example.com/admin 200 123 10"),
        (FuzzerWfuzzTool, FuzzerWfuzzArgs, {"target": "http://example.com", "wordlist": "list.txt"}, "00001 200 123 10 1"),
        # Proxy
        (MitmProxyTool, MitmProxyArgs, {"target": "http://example.com"}, '{"flows": []}'),
    ]


@pytest.mark.parametrize("tool_cls,args_cls,kwargs,stdout", _tool_cases())
def test_execution_model_compliance(tool_cls, args_cls, kwargs, stdout, tmp_path, monkeypatch):
    tool = tool_cls()
    args = args_cls(**kwargs)
    monkeypatch.chdir(tmp_path)

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", _mock_run)

    assert_execution_model_compliance(tool, args_model=args, stdout=stdout)


@pytest.mark.parametrize("tool_cls,args_cls,kwargs,_", _tool_cases())
def test_supports_pty_and_metadata(tool_cls, args_cls, kwargs, _, tmp_path, monkeypatch):
    tool = tool_cls()
    args = args_cls(**kwargs)
    assert tool.supports_pty() is True

    # Ensure run returns ToolResult with populated metadata/artifacts
    monkeypatch.chdir(tmp_path)

    def _mock_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", _mock_run)
    result = tool.run(args)
    assert hasattr(result, "metadata")
    assert isinstance(result.metadata, dict)
