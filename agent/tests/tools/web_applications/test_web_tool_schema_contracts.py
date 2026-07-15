"""Schema contract tests for repaired web application tool wrappers.

These tests keep the LLM-facing schemas honest for the tool wrappers whose
CLI contracts were repaired from current Wfuzz, Skipfish, and Wapiti docs.
"""

from enum import Enum
from typing import Type

import pytest
from pydantic import ValidationError

from agent.tools.web_applications.web_application_fuzzers.wfuzz import (
    WfuzzArgs as WfuzzFuzzerArgs,
)
from agent.tools.web_applications.web_crawlers.gobuster import GobusterArgs
from agent.tools.web_applications.web_crawlers.wfuzz import (
    OutputFormat as WfuzzCrawlerOutputFormat,
    WfuzzArgs as WfuzzCrawlerArgs,
    WfuzzMode,
)
from agent.tools.web_applications.web_vulnerability_scanners.commix import (
    CommixArgs,
    InjectionMethod as CommixInjectionMethod,
    OutputFormat as CommixOutputFormat,
)
from agent.tools.web_applications.web_vulnerability_scanners.skipfish import (
    AuthMode as SkipfishAuthMode,
    OutputFormat as SkipfishOutputFormat,
    SkipfishArgs,
)
from agent.tools.web_applications.web_vulnerability_scanners.sqlmap import SqlmapArgs
from agent.tools.web_applications.web_vulnerability_scanners.wapiti import (
    OutputFormat as WapitiOutputFormat,
    ScanLevel,
    WapitiArgs,
)
from agent.tools.web_applications.web_vulnerability_scanners.xsser import (
    OutputFormat as XsserOutputFormat,
    XsserArgs,
)


def _schema_enum(args_cls: type, enum_name: str) -> list[str]:
    """Return enum values advertised in a Pydantic JSON schema definition."""

    return args_cls.model_json_schema()["$defs"][enum_name]["enum"]


def _enum_values(enum_cls: Type[Enum]) -> list[str]:
    """Return runtime enum values as plain strings."""

    return [member.value for member in enum_cls]


def test_repaired_web_tool_schema_enums_match_runtime_values():
    """Every advertised enum value instantiates successfully."""

    assert _schema_enum(WfuzzCrawlerArgs, "WfuzzMode") == _enum_values(WfuzzMode)
    assert _schema_enum(
        WfuzzCrawlerArgs, "OutputFormat"
    ) == _enum_values(WfuzzCrawlerOutputFormat)
    for mode in WfuzzMode:
        WfuzzCrawlerArgs(target="http://example.com/FUZZ", mode=mode)
    for output_format in WfuzzCrawlerOutputFormat:
        WfuzzCrawlerArgs(
            target="http://example.com/FUZZ",
            output_format=output_format,
        )

    assert _schema_enum(
        CommixArgs, "InjectionMethod"
    ) == _enum_values(CommixInjectionMethod)
    assert _schema_enum(CommixArgs, "OutputFormat") == _enum_values(
        CommixOutputFormat
    )
    for method in CommixInjectionMethod:
        CommixArgs(target="http://example.com", injection_method=method)
    for output_format in CommixOutputFormat:
        CommixArgs(target="http://example.com", output_format=output_format)

    assert _schema_enum(XsserArgs, "OutputFormat") == _enum_values(
        XsserOutputFormat
    )
    for output_format in XsserOutputFormat:
        kwargs = (
            {"output_path": "reports/xsser.xml"}
            if output_format.value == "xml"
            else {}
        )
        XsserArgs(
            target="http://example.com",
            output_format=output_format,
            **kwargs,
        )

    assert _schema_enum(SkipfishArgs, "OutputFormat") == _enum_values(
        SkipfishOutputFormat
    )
    assert _schema_enum(SkipfishArgs, "AuthMode") == _enum_values(SkipfishAuthMode)
    for output_format in SkipfishOutputFormat:
        SkipfishArgs(target="http://example.com", output_format=output_format)
    for auth_mode in SkipfishAuthMode:
        kwargs = {}
        if auth_mode == SkipfishAuthMode.HTTP:
            kwargs = {"auth_user": "user", "auth_pass": "pass"}
        elif auth_mode == SkipfishAuthMode.FORM:
            kwargs = {"auth_form": "http://example.com/login"}
        SkipfishArgs(
            target="http://example.com",
            auth_mode=auth_mode,
            **kwargs,
        )

    assert _schema_enum(WapitiArgs, "OutputFormat") == _enum_values(
        WapitiOutputFormat
    )
    assert _schema_enum(WapitiArgs, "ScanLevel") == _enum_values(ScanLevel)
    for output_format in WapitiOutputFormat:
        WapitiArgs(target="http://example.com", output_format=output_format)
    for scan_level in ScanLevel:
        WapitiArgs(target="http://example.com", scan_level=scan_level)


def test_repaired_web_tool_schemas_do_not_expose_removed_options():
    """Removed unsupported values must not appear as selectable schema options."""

    assert _schema_enum(WfuzzCrawlerArgs, "WfuzzMode") == ["directory"]
    assert _schema_enum(CommixArgs, "OutputFormat") == ["text"]
    assert _schema_enum(XsserArgs, "OutputFormat") == ["text", "xml"]
    assert "injection_method" not in XsserArgs.model_json_schema()["properties"]
    assert _schema_enum(SkipfishArgs, "OutputFormat") == ["html"]
    assert _schema_enum(SkipfishArgs, "AuthMode") == ["none", "form", "http"]


@pytest.mark.parametrize(
    ("args_cls", "kwargs"),
    [
        (GobusterArgs, {"wordlist": "list.txt", "show_length": True}),
        (WfuzzCrawlerArgs, {"unsupported_mode_flag": True}),
        (WfuzzFuzzerArgs, {"debug": True}),
        (SqlmapArgs, {"output_format": "json"}),
        (CommixArgs, {"threads": 10}),
        (WapitiArgs, {"cookies": "session=abc"}),
        (WapitiArgs, {"threads": 10}),
        (XsserArgs, {"cookies": "session=abc"}),
        (XsserArgs, {"injection_method": "post"}),
        (SkipfishArgs, {"threads": 10}),
    ],
)
def test_repaired_web_tool_schemas_reject_removed_arguments(args_cls, kwargs):
    """Removed unsupported options must fail validation instead of being ignored."""

    with pytest.raises(ValidationError):
        args_cls(target="http://example.com", **kwargs)
