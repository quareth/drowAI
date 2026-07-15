"""Unit tests for compact tool-output compression behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from agent.graph.compression.compressor import compress_tool_output
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from agent.graph.compression.deterministic import registry as deterministic_registry
from agent.graph.compression.deterministic.contracts import (
    CompressionInput,
    DeterministicCompressionResult,
)
from agent.graph.compression.deterministic.http import HTTP_REQUEST_TOOL_ID
from agent.graph.compression.deterministic.network_discovery import NMAP_TOOL_ID
from agent.graph.compression.deterministic.registry import register_adapter
from agent.graph.compression.deterministic.credential_attack import HYDRA_TOOL_ID


class _PromptCapturingLLMClient:
    """Minimal compressor LLM client that records prompts if called."""

    model = "gpt-4.1"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> Any:  # noqa: ANN401
        self.prompts.append(f"{system_prompt}\n{user_prompt}")
        return SimpleNamespace(
            content="",
            structured_output={
                "summary": "Captured fallback summary.",
                "key_findings": ["captured fallback finding"],
                "structured_signals": [],
                "decision_evidence": ["captured fallback evidence"],
                "lossiness_risk": "medium",
            },
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                model="gpt-4.1",
                provider="test",
                api_surface="test",
            ),
        )


def _base_raw_result(**overrides: Any) -> Dict[str, Any]:
    raw: Dict[str, Any] = {
        "status": "success",
        "success": True,
        "exit_code": 0,
        "stdout": "scan complete\nopen port 22",
        "stderr": "",
        "parameters": {"target": "127.0.0.1"},
    }
    raw.update(overrides)
    return raw


@pytest.mark.asyncio
async def test_compress_tool_output_success_returns_valid_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful compression returns canonical compact envelope fields."""

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="Nmap found one open SSH port.",
            key_findings=["Port 22/tcp open"],
            next_actions=["Run service version detection"],
            structured_signals=[{"type": "service", "port": 22, "service": "ssh"}],
            decision_evidence=["22/tcp open ssh"],
            lossiness_risk="low",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    llm_client = SimpleNamespace(model="gpt-4o-mini")
    result = await compress_tool_output(
        tool_name="network.nmap_scan",
        raw_result=_base_raw_result(),
        artifact_path="/workspace/artifacts/tool.txt",
        execution_id="exec-123",
        llm_client=llm_client,
    )
    compact = result.compact_output
    payload = compact.to_dict()

    assert payload["schema_version"] == "2.0"
    assert payload["tool"] == "network.nmap_scan"
    assert payload["status"] == "success"
    assert payload["success"] is True
    assert payload["summary"] == "Nmap found one open SSH port."
    assert payload["key_findings"] == ["Port 22/tcp open"]
    assert payload["report_recommendations"] == []
    assert payload["structured_signals"] == [{"type": "service", "port": 22, "service": "ssh"}]
    assert payload["decision_evidence"] == ["22/tcp open ssh"]
    assert payload["lossiness_risk"] == "low"
    assert payload["compression"]["source"] == "llm"
    assert result.usage_record is not None
    assert result.usage_record["source"] == "tool_output_compressor"
    assert result.usage_record["request_mode"] == "non_streaming"


@pytest.mark.asyncio
async def test_compress_tool_output_failed_result_uses_deterministic_fallback() -> None:
    """Failed tool result should still produce deterministic compact envelope."""
    result = await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(
            status="error",
            success=False,
            exit_code=1,
            stdout="",
            stderr="permission denied\ncannot open file",
        ),
        artifact_path=None,
        execution_id=None,
        llm_client=None,
    )
    compact = result.compact_output

    assert result.usage_record is None
    assert compact.success is False
    assert compact.status == "error"
    assert compact.exit_code == 1
    assert compact.compression is not None
    assert compact.compression.source == "deterministic"
    assert len(compact.errors) == 1
    assert "permission denied" in compact.errors[0].lower()


@pytest.mark.asyncio
async def test_compress_tool_output_failure_errors_are_condensed_not_raw_dump() -> None:
    """Compact errors must stay bounded and avoid raw multiline stderr dumps."""

    multiline_stderr = (
        "Traceback (most recent call last):\n"
        "  File \"/app/backend/migrations/env.py\", line 120, in <module>\n"
        "sqlalchemy.exc.NotSupportedError: extension \"vector\" is not available\n"
        "DETAIL: Could not open extension control file\n"
        "HINT: The extension must first be installed on the system where PostgreSQL is running."
    )
    result = await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(
            status="error",
            success=False,
            exit_code=1,
            stdout="",
            stderr=multiline_stderr,
        ),
        artifact_path=None,
        execution_id=None,
        llm_client=None,
    )
    compact = result.compact_output

    assert compact.success is False
    assert len(compact.errors) == 1
    assert "\n" not in compact.errors[0]
    assert "traceback" not in compact.errors[0].lower()


@pytest.mark.asyncio
async def test_compress_tool_output_prefers_metadata_compact_key_findings() -> None:
    """Tool-authored compact findings should use the deterministic lane."""
    result = await compress_tool_output(
        tool_name="information_gathering.network_discovery.fping",
        raw_result=_base_raw_result(
            status="success",
            success=True,
            exit_code=1,
            stdout=(
                "172.17.0.1\n"
                "172.17.0.2\n"
                "172.17.0.3\n"
                "172.17.0.4\n"
            ),
            stderr=(
                "     254 targets\n"
                "       4 alive\n"
                "     250 unreachable\n"
                "       0 unknown addresses\n"
                "     500 timeouts (waiting for response)\n"
            ),
            metadata={
                "compact_key_findings": [
                    "172.17.0.1",
                    "172.17.0.2",
                    "172.17.0.3",
                    "172.17.0.4",
                ],
                "unresponsive_count": 250,
            },
        ),
        artifact_path=None,
        execution_id=None,
        llm_client=None,
    )

    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.key_findings[:4] == [
        "172.17.0.1",
        "172.17.0.2",
        "172.17.0.3",
        "172.17.0.4",
    ]
    assert "254 targets" not in deterministic.key_findings
    assert result.compact_output is result.llm_compact_output


@pytest.mark.asyncio
async def test_compress_tool_output_prefers_metadata_compact_summary_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool-authored compact PCAP fields should be independent from LLM compression."""

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="LLM summary that missed the packet proof.",
            key_findings=["LLM generic finding"],
            next_actions=[],
            structured_signals=[],
            decision_evidence=["LLM generic evidence"],
            lossiness_risk="medium",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="sniffing_spoofing.network_sniffers.tshark",
        raw_result=_base_raw_result(
            stdout='{"schema_version":"pcap.compact.v1"}',
            artifacts=[
                {
                    "artifact_id": "pcap-artifact",
                    "path": (
                        "https://objects.example.invalid/private/capture.pcap"
                        "?X-Amz-Signature=dummy-signature"
                    ),
                    "artifact_kind": "object_store",
                    "label": "PCAP capture",
                    "relative_path": "artifacts/capture.pcap",
                },
                {
                    "artifact_id": "pcap-object-key",
                    "path": "tenant-a/task-123/private/capture.pcap",
                    "artifact_kind": "object_store",
                    "label": "Object key",
                    "relative_path": "tenant-a/task-123/private/capture.pcap",
                },
            ],
            metadata={
                "compact_summary": "PCAP compact analysis parsed 2 packets, 2 hosts, 1 conversations.",
                "compact_key_findings": [
                    "Secret exposure: authorization_header in http.authorization frame=1."
                ],
                "compact_decision_evidence": [
                    "Secret exposure: authorization_header in http.authorization frame=1. proof=Bearer raw-token"
                ],
            },
        ),
        artifact_path=None,
        execution_id="exec-pcap",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert result.compact_output.summary == "LLM summary that missed the packet proof."
    assert result.compact_output.key_findings == ["LLM generic finding"]
    assert result.compact_output.decision_evidence == ["LLM generic evidence"]
    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.summary.startswith("PCAP compact analysis parsed")
    assert deterministic.key_findings == [
        "Secret exposure: authorization_header in http.authorization frame=1."
    ]
    assert deterministic.decision_evidence[0] == (
        "Secret exposure: authorization_header in http.authorization frame=1. proof=Bearer raw-token"
    )
    refs = deterministic.to_dict()["artifact_refs"]
    assert refs == [
        {
            "path": "artifacts/capture.pcap",
            "artifact_id": "pcap-artifact",
            "execution_id": "exec-pcap",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": "object_store",
            "label": "PCAP capture",
            "relative_path": "artifacts/capture.pcap",
        },
        {
            "path": "artifact://pcap-object-key",
            "artifact_id": "pcap-object-key",
            "execution_id": "exec-pcap",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": "object_store",
            "label": "Object key",
            "relative_path": None,
        },
    ]
    assert "X-Amz-Signature" not in str(refs)
    assert "tenant-a/task-123/private/capture.pcap" not in str(refs)
    assert result.compact_output.compression is not None
    assert result.compact_output.compression.source == "llm"
    assert deterministic.compression is not None
    assert deterministic.compression.source == "deterministic"


@pytest.mark.asyncio
async def test_compress_tool_output_preserves_current_merge_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metadata compact fields and locator evidence move to deterministic lane."""

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="Processor summary should lose to metadata.",
            key_findings=["Processor finding should lose to metadata."],
            next_actions=["This recommendation must not be promoted."],
            structured_signals=[{"type": "service", "port": 22, "service": "ssh"}],
            decision_evidence=["Processor evidence should remain last."],
            lossiness_risk="low",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.merge_no_adapter",
        raw_result=_base_raw_result(
            stdout="Located 1 matches\nartifacts/service.txt:7:service=ssh",
            metadata={
                "compact_summary": "Metadata compact summary wins.",
                "compact_key_findings": ["Metadata compact finding wins."],
                "compact_decision_evidence": ["Metadata evidence stays first."],
                "compact_structured_signals": [
                    {"type": "service", "port": 443, "service": "https"}
                ],
                "structured_signals": [
                    {"type": "service", "port": 443, "service": "https"}
                ],
                "fs_search_text": {
                    "matches": [
                        {
                            "path": "artifacts/service.txt",
                            "line": 7,
                            "column": 1,
                            "snippet": "service=ssh",
                        }
                    ],
                    "truncated": False,
                },
            },
        ),
        artifact_path=None,
        execution_id="exec-merge-contract",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    compact = result.compact_output
    deterministic = result.deterministic_compact_output

    assert deterministic is not None
    assert compact.summary == "Processor summary should lose to metadata."
    assert compact.key_findings == ["Processor finding should lose to metadata."]
    assert compact.decision_evidence == ["Processor evidence should remain last."]
    assert deterministic.summary == "Metadata compact summary wins."
    assert deterministic.key_findings == ["Metadata compact finding wins."]
    assert deterministic.decision_evidence == [
        "Metadata evidence stays first.",
        "artifacts/service.txt:7:service=ssh",
    ]
    assert compact.structured_signals == [{"type": "service", "port": 22, "service": "ssh"}]
    assert deterministic.structured_signals == [
        {"type": "service", "port": 443, "service": "https"}
    ]
    assert compact.report_recommendations == []
    assert compact.compression is not None
    assert compact.compression.source == "llm"


@pytest.mark.asyncio
async def test_compress_tool_output_preserves_unbounded_key_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compact envelope should not truncate key_findings to five items."""

    findings = [f"target-{index}: Drupal {index}.x (PHP In-Memory)" for index in range(12)]

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="Metasploit module exposes 12 exploit targets.",
            key_findings=findings,
            next_actions=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="exploitation_tools.metasploit.inspect_module",
        raw_result=_base_raw_result(stdout="show targets output"),
        artifact_path=None,
        execution_id="exec-targets",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert result.compact_output.key_findings == findings


@pytest.mark.asyncio
async def test_compress_tool_output_preserves_filesystem_search_line_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem search matches should become deterministic lane evidence."""

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="Search found service evidence.",
            key_findings=["443/tcp closed"],
            next_actions=[],
            structured_signals=[],
            decision_evidence=["443 service evidence"],
            lossiness_risk="low",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    expected = (
        "artifacts/nmap.xml:13:<ports><port protocol=\"tcp\" portid=\"443\">"
        "<state state=\"closed\"/><service name=\"_https_\"/></port>"
    )
    result = await compress_tool_output(
        tool_name="filesystem.search_text",
        raw_result=_base_raw_result(
            stdout=(
                "Located 1 matches\n"
                "artifacts/nmap.xml:13:<ports><port protocol=\"tcp\" portid=\"443\">"
                "<state state=\"closed\"/><service name=\"_https_\"/></port>"
            ),
            metadata={
                "fs_search_text": {
                    "matches": [
                        {
                            "path": "artifacts/nmap.xml",
                            "line": 13,
                            "column": 1,
                            "snippet": (
                                "<ports><port protocol=\"tcp\" portid=\"443\">"
                                "<state state=\"closed\"/><service name=\"_https_\"/></port>"
                            ),
                        }
                    ],
                    "truncated": False,
                }
            },
        ),
        artifact_path=None,
        execution_id=None,
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert result.compact_output.decision_evidence == ["443 service evidence"]
    assert deterministic.decision_evidence[0] == expected


@pytest.mark.asyncio
async def test_compress_tool_output_preserves_raw_line_locator_evidence() -> None:
    """Read-file line evidence metadata should be retained in deterministic lane."""

    result = await compress_tool_output(
        tool_name="filesystem.read_file",
        raw_result=_base_raw_result(
            stdout=(
                "6:<scaninfo type=\"connect\" protocol=\"tcp\" numservices=\"1\" services=\"443\"/>\n"
                "10:<address addr=\"127.0.0.1\" addrtype=\"ipv4\"/>"
            ),
            metadata={
                "fs_read": {
                    "line_evidence": [
                        "6:<scaninfo type=\"connect\" protocol=\"tcp\" numservices=\"1\" services=\"443\"/>",
                        "10:<address addr=\"127.0.0.1\" addrtype=\"ipv4\"/>",
                    ]
                }
            },
        ),
        artifact_path=None,
        execution_id=None,
        llm_client=None,
    )

    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert result.compact_output.decision_evidence == []
    assert deterministic.decision_evidence == [
        "6:<scaninfo type=\"connect\" protocol=\"tcp\" numservices=\"1\" services=\"443\"/>",
        "10:<address addr=\"127.0.0.1\" addrtype=\"ipv4\"/>",
    ]


@pytest.mark.asyncio
async def test_compress_tool_output_does_not_promote_non_filesystem_colon_lines() -> None:
    """Generic command output should not treat every line-number-like prefix as evidence."""

    result = await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(stdout="10:not a filesystem evidence locator"),
        artifact_path=None,
        execution_id=None,
        llm_client=None,
    )

    assert result.compact_output.decision_evidence == []


@pytest.mark.asyncio
async def test_compress_tool_output_populates_artifact_references() -> None:
    """Compression should map artifact path and string artifacts into references."""
    result = await compress_tool_output(
        tool_name="filesystem.read_file",
        raw_result=_base_raw_result(
            artifacts=[
                "/workspace/artifacts/secondary.txt",
            ]
        ),
        artifact_path="/workspace/artifacts/primary.txt",
        execution_id="exec-456",
        llm_client=None,
    )
    compact = result.compact_output

    assert result.usage_record is None
    refs = compact.to_dict()["artifact_refs"]
    assert len(refs) == 2
    assert refs[0]["path"] == "/workspace/artifacts/primary.txt"
    assert refs[0]["execution_id"] == "exec-456"
    assert refs[1]["path"] == "/workspace/artifacts/secondary.txt"
    assert refs[1]["execution_id"] == "exec-456"


@pytest.mark.asyncio
async def test_compress_tool_output_preserves_current_artifact_mapping_fields() -> None:
    """Current artifact mappings pass accepted fields through unchanged."""
    result = await compress_tool_output(
        tool_name="filesystem.read_file",
        raw_result=_base_raw_result(
            artifacts=[
                {
                    "artifact_id": "artifact-1",
                    "tool_call_id": "call-1",
                    "tool_name": "filesystem.read_file",
                    "artifact_kind": "raw_output",
                    "label": "Read output",
                    "path": "/workspace/artifacts/structured.json",
                    "relative_path": "artifacts/structured.json",
                },
                {
                    "artifact_id": "artifact-2",
                    "artifact_path": "/workspace/artifacts/alternate.txt",
                    "relative_path": "artifacts/alternate.txt",
                },
            ]
        ),
        artifact_path=None,
        execution_id="exec-structured-artifacts",
        llm_client=None,
    )

    refs = result.compact_output.to_dict()["artifact_refs"]

    assert refs == [
        {
            "path": "/workspace/artifacts/structured.json",
            "artifact_id": "artifact-1",
            "execution_id": "exec-structured-artifacts",
            "tool_call_id": "call-1",
            "tool_name": "filesystem.read_file",
            "artifact_kind": "raw_output",
            "label": "Read output",
            "relative_path": "artifacts/structured.json",
        },
        {
            "path": "/workspace/artifacts/alternate.txt",
            "artifact_id": "artifact-2",
            "execution_id": "exec-structured-artifacts",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": None,
            "label": None,
            "relative_path": "artifacts/alternate.txt",
        },
    ]


@pytest.mark.asyncio
async def test_compress_tool_output_sanitizes_unsafe_artifact_refs() -> None:
    """Signed URLs and object keys must not enter compact artifact refs."""
    signed_url = (
        "https://objects.example.invalid/private/task-output.json"
        "?X-Amz-Signature=dummy-signature&X-Amz-Credential=dummy-credential"
    )
    object_key = "tenant-a/task-123/private/task-output.json"

    result = await compress_tool_output(
        tool_name="http.download",
        raw_result=_base_raw_result(
            artifacts=[
                {
                    "artifact_id": "signed-artifact",
                    "path": signed_url,
                    "artifact_kind": "object_store",
                    "label": "Signed object URL",
                    "relative_path": "artifacts/task-output.json",
                },
                {
                    "artifact_id": "object-key-artifact",
                    "path": object_key,
                    "artifact_kind": "object_store",
                    "label": "Object key",
                    "relative_path": object_key,
                },
            ]
        ),
        artifact_path=None,
        execution_id="exec-unsafe-artifact",
        llm_client=None,
    )

    refs = result.compact_output.to_dict()["artifact_refs"]

    assert refs == [
        {
            "path": "artifacts/task-output.json",
            "artifact_id": "signed-artifact",
            "execution_id": "exec-unsafe-artifact",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": "object_store",
            "label": "Signed object URL",
            "relative_path": "artifacts/task-output.json",
        },
        {
            "path": "artifact://object-key-artifact",
            "artifact_id": "object-key-artifact",
            "execution_id": "exec-unsafe-artifact",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": "object_store",
            "label": "Object key",
            "relative_path": None,
        },
    ]
    assert signed_url not in str(refs)
    assert object_key not in str(refs)


@pytest.mark.asyncio
async def test_compress_tool_output_dedupes_by_sanitized_artifact_path() -> None:
    """Duplicate unsafe refs collapse after sanitization to the same stable path."""
    signed_url_a = "https://objects.example.invalid/a?X-Amz-Signature=dummy-a"
    signed_url_b = "https://objects.example.invalid/b?X-Amz-Signature=dummy-b"

    result = await compress_tool_output(
        tool_name="http.download",
        raw_result=_base_raw_result(
            artifacts=[
                {
                    "artifact_id": "artifact-a",
                    "path": signed_url_a,
                    "artifact_kind": "object_store",
                    "relative_path": "artifacts/result.json",
                },
                {
                    "artifact_id": "artifact-b",
                    "path": signed_url_b,
                    "artifact_kind": "object_store",
                    "relative_path": "artifacts/result.json",
                },
            ]
        ),
        artifact_path=None,
        execution_id="exec-dedupe-artifact",
        llm_client=None,
    )

    refs = result.compact_output.to_dict()["artifact_refs"]

    assert refs == [
        {
            "path": "artifacts/result.json",
            "artifact_id": "artifact-a",
            "execution_id": "exec-dedupe-artifact",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": "object_store",
            "label": None,
            "relative_path": "artifacts/result.json",
        }
    ]


@pytest.mark.asyncio
async def test_compress_tool_output_llm_processor_exception_triggers_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM processing exception should force deterministic compression source."""

    async def _raise_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        raise RuntimeError("processor boom")

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _raise_stub,
    )

    llm_client = SimpleNamespace(model="gpt-4o-mini")
    result = await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(stdout="ok"),
        artifact_path=None,
        execution_id=None,
        llm_client=llm_client,
    )
    compact = result.compact_output

    assert result.usage_record is None
    assert compact.compression is not None
    assert compact.compression.source == "deterministic"
    assert compact.compression.fallback_reason == "processor_exception"
    assert compact.summary != ""


@pytest.mark.asyncio
async def test_compress_tool_output_propagates_provider_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compression boundary must preserve a typed provider refusal."""
    refusal = LLMRefusalError(
        "declined",
        outcome=LLMRefusalOutcome(provider="openai", model="gpt-4o-mini"),
    )

    async def _raise_refusal(*_args: Any, **_kwargs: Any) -> Any:
        raise refusal

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _raise_refusal,
    )

    with pytest.raises(LLMRefusalError) as exc_info:
        await compress_tool_output(
            tool_name="shell.exec",
            raw_result=_base_raw_result(stdout="line\n" * 100),
            artifact_path=None,
            execution_id=None,
            llm_client=SimpleNamespace(model="gpt-4o-mini"),
        )

    assert exc_info.value is refusal


@pytest.mark.asyncio
async def test_compress_tool_output_captures_compression_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compression metadata should capture source/model/token usage."""

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="Result summary",
            key_findings=["Finding A"],
            next_actions=["Action A"],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="medium",
            usage={
                "prompt_tokens": 5,
                "completion_tokens": 7,
                "total_tokens": 12,
                "model": "gpt-4.1",
                "provider": "openai",
                "api_surface": "responses",
                "cache_reporting": "reported",
            },
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    llm_client = SimpleNamespace(model="gpt-4.1")
    result = await compress_tool_output(
        tool_name="network.nmap_scan",
        raw_result=_base_raw_result(),
        artifact_path=None,
        execution_id="exec-999",
        llm_client=llm_client,
    )
    compact = result.compact_output

    assert result.usage_record is not None
    assert result.usage_record["source"] == "tool_output_compressor"
    assert result.usage_record["request_mode"] == "non_streaming"
    assert result.usage_record["model"] == "gpt-4.1"
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.model == "gpt-4.1"
    assert compact.compression.token_usage == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
    }


@pytest.mark.asyncio
async def test_compress_tool_output_no_adapter_fallback_preserves_current_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing deterministic adapter should keep current processor-driven output."""

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="Processor summary remains authoritative.",
            key_findings=["Processor finding remains authoritative."],
            next_actions=[],
            structured_signals=[{"type": "service", "port": 22, "service": "ssh"}],
            decision_evidence=["Processor evidence remains authoritative."],
            lossiness_risk="low",
            usage={
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
                "model": "gpt-4o-mini",
            },
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.no_adapter",
        raw_result=_base_raw_result(stdout="processor fallback input"),
        artifact_path="/workspace/artifacts/no-adapter.txt",
        execution_id="exec-no-adapter",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    compact = result.compact_output

    assert compact.summary == "Processor summary remains authoritative."
    assert compact.key_findings == ["Processor finding remains authoritative."]
    assert compact.structured_signals == [{"type": "service", "port": 22, "service": "ssh"}]
    assert compact.decision_evidence == ["Processor evidence remains authoritative."]
    assert compact.lossiness_risk == "low"
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.fallback_reason is None
    assert compact.compression.token_usage == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
    assert result.usage_record is not None
    assert result.usage_record["model"] == "gpt-4o-mini"
    assert result.usage_record["prompt_tokens"] == 3


@pytest.mark.parametrize(
    ("tool_name", "raw_result", "artifact_path", "expected_summary"),
    [
        (
            HTTP_REQUEST_TOOL_ID,
            _base_raw_result(
                stdout="<html>RAW_BODY_SHOULD_NOT_BE_PROMOTED</html>",
                parameters={"target": "https://example.test/login", "method": "POST"},
                metadata={
                    "complete_fixture": {
                        "summary": "HTTP complete fixture summary.",
                        "finding": "http complete fixture finding",
                        "evidence": "http complete fixture evidence",
                        "signal_key": "fixture_family",
                        "signal_value": "http",
                    },
                    "status_code": 302,
                    "effective_url": "https://example.test/home",
                    "request_method": "POST",
                    "content_type": "text/html",
                },
            ),
            "/workspace/artifacts/http-complete.txt",
            "HTTP complete fixture summary.",
        ),
        (
            NMAP_TOOL_ID,
            _base_raw_result(
                stdout="<nmaprun>RAW_XML_SHOULD_NOT_BE_PROMOTED</nmaprun>",
                parameters={"target": "10.0.0.5"},
                metadata={
                    "complete_fixture": {
                        "summary": "Nmap complete fixture summary.",
                        "finding": "nmap complete fixture finding",
                        "evidence": "nmap complete fixture evidence",
                        "signal_key": "fixture_family",
                        "signal_value": "nmap",
                    },
                    "hosts_total": 1,
                    "hosts_up": 1,
                    "open_ports": [
                        {
                            "host": "10.0.0.5",
                            "port": 443,
                            "protocol": "tcp",
                            "service": "https",
                            "status": "open",
                        }
                    ],
                },
            ),
            "/workspace/artifacts/nmap-complete.xml",
            "Nmap complete fixture summary.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_compress_tool_output_deterministic_complete_pentest_tools_augments_processor(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    raw_result: Dict[str, Any],
    artifact_path: str,
    expected_summary: str,
) -> None:
    """Complete deterministic pentest-role fixtures use a separate lane."""
    adapter_calls: list[str] = []
    processor_calls: list[str] = []
    captured_metadata: list[Dict[str, Any]] = []

    def _complete_adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
        adapter_calls.append(input_data.tool_name)
        metadata = input_data.raw_result.get("metadata")
        fixture = metadata.get("complete_fixture") if isinstance(metadata, dict) else {}
        if not isinstance(fixture, dict):
            fixture = {}
        return DeterministicCompressionResult(
            summary=str(fixture.get("summary") or ""),
            key_findings=(str(fixture.get("finding") or ""),),
            structured_signals=(
                {
                    "type": "kv_pair",
                    "key": str(fixture.get("signal_key") or "fixture"),
                    "value": str(fixture.get("signal_value") or input_data.tool_name),
                },
            ),
            decision_evidence=(str(fixture.get("evidence") or ""),),
            lossiness_risk="low",
            completeness="complete",
        )

    async def _process_output_stub(
        self,
        tool_name: str,
        raw_output: str,
        metadata: Dict[str, Any],
    ):  # noqa: ANN001
        processor_calls.append(tool_name)
        captured_metadata.append(dict(metadata))
        return SimpleNamespace(
            summary=f"Processor summary for {tool_name}.",
            key_findings=["Processor finding wins."],
            next_actions=[],
            structured_signals=[{"type": "kv_pair", "key": "source", "value": "processor"}],
            decision_evidence=["Processor evidence wins."],
            lossiness_risk="medium",
            analysis_source="llm",
            usage={
                "prompt_tokens": 6,
                "completion_tokens": 7,
                "total_tokens": 13,
                "model": "gpt-4.1",
            },
        )

    monkeypatch.setitem(deterministic_registry._ADAPTERS, tool_name, _complete_adapter)
    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name=tool_name,
        raw_result=raw_result,
        artifact_path=artifact_path,
        execution_id="exec-complete-real-tool-fixture",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    compact = result.compact_output
    fixture = raw_result["metadata"]["complete_fixture"]

    assert adapter_calls == [tool_name]
    assert processor_calls == [tool_name]
    assert "deterministic_analysis" not in captured_metadata[0]
    assert compact.summary == f"Processor summary for {tool_name}."
    assert compact.key_findings == ["Processor finding wins."]
    assert compact.structured_signals == [
        {
            "type": "kv_pair",
            "key": "source",
            "value": "processor",
        }
    ]
    assert compact.decision_evidence == ["Processor evidence wins."]
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.fallback_reason is None
    assert compact.compression.token_usage == {
        "prompt_tokens": 6,
        "completion_tokens": 7,
        "total_tokens": 13,
    }
    assert result.usage_record is not None
    assert result.usage_record["total_tokens"] == 13
    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.summary == expected_summary
    assert deterministic.key_findings == [fixture["finding"]]
    assert deterministic.decision_evidence == [fixture["evidence"]]


@pytest.mark.asyncio
async def test_http_request_compact_output_prefers_parsed_page_facts_over_raw_body() -> None:
    """HTTP deterministic page facts should be separate from raw LLM lane."""

    result = await compress_tool_output(
        tool_name=HTTP_REQUEST_TOOL_ID,
        raw_result=_base_raw_result(
            stdout="""HTTP/1.1 200 OK
Server: gunicorn
Content-Type: text/html; charset=utf-8

<!doctype html>
<html>
  <head><title>Security Dashboard</title></head>
  <body>
    <p>RAW_SECRET_BODY_LINE_SHOULD_NOT_BE_PROMOTED</p>
    <a href="/capture">Capture</a>
    <a href="/download/1">Download</a>
  </body>
</html>
""",
            parameters={"target": "https://example.test/", "method": "GET"},
            metadata={
                "status_code": 200,
                "effective_url": "https://example.test/",
                "request_method": "GET",
                "content_type": "text/html; charset=utf-8",
                "content_length": 400,
                "body_captured": True,
                "response_headers": {"Server": "gunicorn"},
            },
        ),
        artifact_path=None,
        execution_id="exec-http-page-facts",
        llm_client=None,
    )

    compact = result.compact_output
    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    rendered = str(deterministic.to_dict())

    assert compact.summary == "HTTP/1.1 200 OK"
    assert deterministic.summary == (
        "HTTP GET https://example.test/; status 200; "
        "content_type text/html; charset=utf-8; bytes 400"
    )
    assert 'page title: "Security Dashboard"' in deterministic.key_findings
    assert "internal links: /capture, /download/1" in deterministic.key_findings
    assert "download links: /download/1" in deterministic.key_findings
    assert "RAW_SECRET_BODY_LINE_SHOULD_NOT_BE_PROMOTED" not in rendered
    assert compact.compression is not None
    assert compact.compression.source == "deterministic"
    assert deterministic.compression is not None
    assert deterministic.compression.source == "deterministic"


@pytest.mark.asyncio
async def test_compress_tool_output_utility_catalog_role_skips_deterministic_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Utility-role tools should use the processor path even with a registered adapter."""
    service_access_tool_id = "service_access.ftp_login"
    calls: list[str] = []

    def _deterministic_should_not_run(input_data: CompressionInput) -> DeterministicCompressionResult:
        calls.append(f"adapter:{input_data.tool_name}")
        raise AssertionError("utility tools must skip the deterministic registry")

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        calls.append(f"processor:{tool_name}")
        return SimpleNamespace(
            summary="Processor utility summary.",
            key_findings=["Processor utility finding."],
            next_actions=[],
            structured_signals=[{"type": "kv_pair", "key": "source", "value": "processor"}],
            decision_evidence=["Processor utility evidence."],
            lossiness_risk="medium",
            analysis_source="llm",
            usage={
                "prompt_tokens": 4,
                "completion_tokens": 5,
                "total_tokens": 9,
                "model": "gpt-4.1",
            },
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.compress_deterministically",
        _deterministic_should_not_run,
    )
    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name=service_access_tool_id,
        raw_result=_base_raw_result(
            stdout="login proof complete",
            metadata={
                "operation": "ftp_login",
                "auth_success": True,
                "exit_code": 0,
            },
        ),
        artifact_path=None,
        execution_id="exec-service-access-role-skip",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    compact = result.compact_output

    assert calls == [f"processor:{service_access_tool_id}"]
    assert compact.summary == "Processor utility summary."
    assert compact.key_findings == ["Processor utility finding."]
    assert compact.structured_signals == [
        {"type": "kv_pair", "key": "source", "value": "processor"}
    ]
    assert compact.decision_evidence == ["Processor utility evidence."]
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.fallback_reason is None
    assert compact.compression.token_usage == {
        "prompt_tokens": 4,
        "completion_tokens": 5,
        "total_tokens": 9,
    }
    assert result.usage_record is not None
    assert result.usage_record["total_tokens"] == 9


@pytest.mark.asyncio
async def test_compress_tool_output_complete_adapter_augments_processor_and_keeps_metadata_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete deterministic adapters should not augment the generic processor path."""
    calls: list[str] = []
    captured_metadata: Dict[str, Any] = {}

    def _adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
        calls.append(f"adapter:{input_data.tool_name}")
        return DeterministicCompressionResult(
            summary="Adapter summary is available.",
            key_findings=("Adapter finding is available.",),
            structured_signals=({"type": "service", "port": 8080, "service": "http"},),
            decision_evidence=("Adapter evidence is available.",),
            lossiness_risk="high",
            completeness="complete",
        )

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        calls.append(f"processor:{tool_name}")
        captured_metadata.update(metadata)
        return SimpleNamespace(
            summary="Processor summary is available.",
            key_findings=["Processor finding is available."],
            next_actions=[],
            structured_signals=[{"type": "service", "port": 443, "service": "https"}],
            decision_evidence=["Processor evidence is available."],
            lossiness_risk="low",
            analysis_source="llm",
            usage={
                "prompt_tokens": 9,
                "completion_tokens": 10,
                "total_tokens": 19,
                "model": "gpt-4.1",
            },
        )

    register_adapter("registry_wiring_tests.complete_adapter", _adapter)
    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.complete_adapter",
        raw_result=_base_raw_result(
            stdout="adapter input",
            metadata={
                "compact_summary": "Metadata compact summary wins.",
                "compact_key_findings": ["Metadata compact finding wins."],
                "compact_decision_evidence": ["Metadata compact evidence wins."],
            },
        ),
        artifact_path="/workspace/artifacts/complete-adapter.txt",
        execution_id="exec-complete-adapter",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    compact = result.compact_output

    assert calls == [
        "adapter:registry_wiring_tests.complete_adapter",
        "processor:registry_wiring_tests.complete_adapter",
    ]
    assert "deterministic_analysis" not in captured_metadata
    assert compact.summary == "Processor summary is available."
    assert compact.key_findings == ["Processor finding is available."]
    assert compact.structured_signals == [{"type": "service", "port": 443, "service": "https"}]
    assert compact.decision_evidence == ["Processor evidence is available."]
    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.summary == "Metadata compact summary wins."
    assert deterministic.key_findings == ["Metadata compact finding wins."]
    assert deterministic.structured_signals == [
        {"type": "service", "port": 8080, "service": "http"}
    ]
    assert deterministic.decision_evidence == [
        "Metadata compact evidence wins.",
        "Adapter evidence is available.",
    ]
    assert compact.lossiness_risk == "low"
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.token_usage == {
        "prompt_tokens": 9,
        "completion_tokens": 10,
        "total_tokens": 19,
    }
    assert result.usage_record is not None


@pytest.mark.asyncio
async def test_compress_tool_output_deterministic_partial_adapter_augments_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial deterministic fields should use a separate lane from LLM output."""
    calls: list[str] = []
    captured_metadata: Dict[str, Any] = {}

    def _adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
        calls.append(f"adapter:{input_data.tool_name}")
        return DeterministicCompressionResult(
            summary="Deterministic summary wins.",
            key_findings=("Deterministic finding wins.",),
            structured_signals=({"type": "service", "port": 8080, "service": "http"},),
            decision_evidence=("Deterministic evidence stays before fill-ins.",),
            lossiness_risk="high",
            completeness="partial",
        )

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        calls.append(f"processor:{tool_name}")
        captured_metadata.update(metadata)
        return SimpleNamespace(
            summary="Processor summary should only fill missing fields.",
            key_findings=["Processor finding should only fill missing fields."],
            next_actions=[],
            structured_signals=[{"type": "service", "port": 443, "service": "https"}],
            decision_evidence=["Processor evidence is an allowed fill-in."],
            lossiness_risk="low",
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 13,
                "total_tokens": 24,
                "model": "gpt-4.1",
            },
        )

    register_adapter("registry_wiring_tests.partial_adapter", _adapter)
    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.partial_adapter",
        raw_result=_base_raw_result(stdout="adapter and processor input"),
        artifact_path="/workspace/artifacts/partial-adapter.txt",
        execution_id="exec-partial-adapter",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    compact = result.compact_output

    assert calls == [
        "adapter:registry_wiring_tests.partial_adapter",
        "processor:registry_wiring_tests.partial_adapter",
    ]
    assert "deterministic_analysis" not in captured_metadata
    assert compact.summary == "Processor summary should only fill missing fields."
    assert compact.key_findings == ["Processor finding should only fill missing fields."]
    assert compact.structured_signals == [{"type": "service", "port": 443, "service": "https"}]
    assert compact.decision_evidence == ["Processor evidence is an allowed fill-in."]
    assert compact.lossiness_risk == "low"
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.fallback_reason is None
    assert compact.compression.token_usage == {
        "prompt_tokens": 11,
        "completion_tokens": 13,
        "total_tokens": 24,
    }
    assert result.usage_record is not None
    assert result.usage_record["model"] == "gpt-4.1"
    assert result.usage_record["total_tokens"] == 24
    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.summary == "Deterministic summary wins."
    assert deterministic.key_findings == ["Deterministic finding wins."]
    assert deterministic.structured_signals == [
        {"type": "service", "port": 8080, "service": "http"}
    ]
    assert deterministic.decision_evidence == [
        "Deterministic evidence stays before fill-ins."
    ]
    assert deterministic.lossiness_risk == "high"


@pytest.mark.asyncio
async def test_compress_tool_output_partial_adapter_keeps_llm_output_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM-backed processor output remains authoritative over deterministic context."""
    calls: list[str] = []
    deterministic_evidence = tuple(
        f"Deterministic evidence {index} wins." for index in range(1, 6)
    )

    def _adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
        calls.append(f"adapter:{input_data.tool_name}")
        return DeterministicCompressionResult(
            summary="Deterministic summary wins all visible slots.",
            key_findings=("Deterministic finding wins all visible slots.",),
            structured_signals=({"type": "service", "port": 8080, "service": "http"},),
            decision_evidence=deterministic_evidence,
            lossiness_risk="high",
            completeness="partial",
        )

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        calls.append(f"processor:{tool_name}")
        return SimpleNamespace(
            summary="Processor summary is hidden by deterministic summary.",
            key_findings=["Processor finding is hidden by deterministic finding."],
            next_actions=[],
            structured_signals=[{"type": "service", "port": 443, "service": "https"}],
            decision_evidence=["Processor evidence is hidden by the evidence limit."],
            lossiness_risk="low",
            analysis_source="llm",
            usage={
                "prompt_tokens": 7,
                "completion_tokens": 8,
                "total_tokens": 15,
                "model": "gpt-4.1",
            },
        )

    register_adapter("registry_wiring_tests.partial_hidden_llm_adapter", _adapter)
    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.partial_hidden_llm_adapter",
        raw_result=_base_raw_result(stdout="adapter and hidden processor input"),
        artifact_path="/workspace/artifacts/partial-hidden-llm-adapter.txt",
        execution_id="exec-partial-hidden-llm-adapter",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    compact = result.compact_output

    assert calls == [
        "adapter:registry_wiring_tests.partial_hidden_llm_adapter",
        "processor:registry_wiring_tests.partial_hidden_llm_adapter",
    ]
    assert compact.summary == "Processor summary is hidden by deterministic summary."
    assert compact.key_findings == ["Processor finding is hidden by deterministic finding."]
    assert compact.structured_signals == [{"type": "service", "port": 443, "service": "https"}]
    assert compact.decision_evidence == ["Processor evidence is hidden by the evidence limit."]
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.fallback_reason is None
    assert compact.compression.token_usage == {
        "prompt_tokens": 7,
        "completion_tokens": 8,
        "total_tokens": 15,
    }
    assert result.usage_record is not None
    assert result.usage_record["source"] == "tool_output_compressor"
    assert result.usage_record["request_mode"] == "non_streaming"
    assert result.usage_record["model"] == "gpt-4.1"
    assert result.usage_record["total_tokens"] == 15


@pytest.mark.asyncio
async def test_compress_tool_output_none_adapter_marks_fallback_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter none results should keep processor output and explain fallback."""

    def _adapter(input_data: CompressionInput) -> DeterministicCompressionResult:
        return DeterministicCompressionResult(completeness="none")

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        return SimpleNamespace(
            summary="Processor summary fills adapter none.",
            key_findings=["Processor finding fills adapter none."],
            next_actions=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            usage={
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "model": "gpt-4.1",
            },
        )

    register_adapter("registry_wiring_tests.none_adapter", _adapter)
    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.none_adapter",
        raw_result=_base_raw_result(stdout="adapter none input"),
        artifact_path=None,
        execution_id="exec-none-adapter",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    compact = result.compact_output

    assert compact.summary == "Processor summary fills adapter none."
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.fallback_reason is None
    assert result.usage_record is not None
    assert result.usage_record["total_tokens"] == 5


@pytest.mark.asyncio
async def test_compress_tool_output_short_shell_output_skips_llm_and_marks_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short shell output should use deterministic processing even with an LLMClient."""
    from agent.context.tool_processor import UniversalToolProcessor

    monkeypatch.setattr(UniversalToolProcessor, "_LLM_BYPASS_MAX_CHARS", 1200)
    monkeypatch.setattr(UniversalToolProcessor, "_LLM_BYPASS_MAX_LINES", 40)

    class _ShouldNotCallLLM:
        model = "gpt-4o-mini"

        async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            raise AssertionError("LLM should not be called for short shell command output")

    result = await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(stdout="a\nb\nc\n", stderr=""),
        artifact_path=None,
        execution_id="exec-skip",
        llm_client=_ShouldNotCallLLM(),
    )
    compact = result.compact_output

    assert result.usage_record is None
    assert compact.compression is not None
    assert compact.compression.source == "deterministic"
    assert compact.compression.fallback_reason == "llm_threshold_bypass"


@pytest.mark.asyncio
async def test_compress_tool_output_bypasses_llm_for_bounded_non_text_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With widened caps, bounded outputs bypass LLM regardless of detected format."""
    from agent.context.tool_processor import UniversalToolProcessor

    # Simulates TOOL_PROCESSOR_LLM_BYPASS_MAX_CHARS/LINES=3000/100; the
    # default caps (1200/40) route this payload through the LLM instead.
    monkeypatch.setattr(UniversalToolProcessor, "_LLM_BYPASS_MAX_CHARS", 3000)
    monkeypatch.setattr(UniversalToolProcessor, "_LLM_BYPASS_MAX_LINES", 100)

    class _ShouldNotCallLLM:
        model = "gpt-4o-mini"

        async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            raise AssertionError("LLM should not be called for bounded tool output")

    target_rows = "\n".join(
        f"    {index}   Drupal {index}.x (PHP In-Memory)" for index in range(50)
    )
    msf_output = (
        "[*] No payload configured, defaulting to php/meterpreter/reverse_tcp\n\n"
        "Exploit targets:\n"
        "=================\n\n"
        "    Id  Name\n"
        "    --  ----\n"
        f"{target_rows}\n"
    )

    assert 1200 < len(msf_output) <= 3000
    assert len([line for line in msf_output.splitlines() if line.strip()]) <= 100

    result = await compress_tool_output(
        tool_name="exploitation_tools.metasploit.inspect_module",
        raw_result=_base_raw_result(stdout=msf_output, stderr=""),
        artifact_path=None,
        execution_id="exec-msf-targets",
        llm_client=_ShouldNotCallLLM(),
    )
    compact = result.compact_output

    assert result.usage_record is None
    assert compact.compression is not None
    assert compact.compression.source == "deterministic"
    assert compact.compression.fallback_reason == "llm_threshold_bypass"
    assert any("[*] No payload configured" in finding for finding in compact.key_findings)
    assert "49   Drupal 49.x (PHP In-Memory)" in compact.key_findings


@pytest.mark.asyncio
async def test_compress_tool_output_hydra_long_output_never_sends_raw_secrets_to_processor() -> None:
    """Hydra LLM lane intentionally receives raw output without deterministic augmentation."""
    from agent.context.tool_processor import UniversalToolProcessor

    raw_password = "HydraRawPassword123"
    raw_bearer = "HYDRA_RAW_BEARER_TOKEN"
    raw_cookie = "HYDRA_RAW_COOKIE"
    raw_parameter_token = "HYDRA_RAW_PARAMETER_TOKEN"
    long_noise = "\n".join(
        f"[DEBUG] request {index} Authorization: Bearer {raw_bearer} Cookie: session={raw_cookie}"
        for index in range(80)
    )
    stdout = (
        "Hydra v9.5 (c) 2023 by van Hauser/THC & David Maciejak\n"
        "[DATA] attacking ssh://10.10.10.5:22/\n"
        f"[22][ssh] host: 10.10.10.5   login: admin   password: {raw_password}\n"
        "1 of 1 target successfully completed, 1 valid password found\n"
        f"{long_noise}\n"
    )
    line_count = len([line for line in stdout.splitlines() if line.strip()])
    assert len(stdout) > UniversalToolProcessor._LLM_BYPASS_MAX_CHARS
    assert line_count > UniversalToolProcessor._LLM_BYPASS_MAX_LINES

    llm_client = _PromptCapturingLLMClient()
    result = await compress_tool_output(
        tool_name=HYDRA_TOOL_ID,
        raw_result=_base_raw_result(
            stdout=stdout,
            stderr="",
            parameters={
                "target": "10.10.10.5",
                "service_type": "ssh",
                "password": raw_parameter_token,
            },
        ),
        artifact_path=None,
        execution_id="exec-hydra-secret-regression",
        llm_client=llm_client,
    )

    compact = result.compact_output
    rendered_prompt = "\n".join(llm_client.prompts)
    assert llm_client.prompts
    assert compact.compression is not None
    assert compact.compression.source == "llm"
    assert compact.compression.token_usage is not None
    assert compact.compression.token_usage["prompt_tokens"] == 1
    assert compact.compression.token_usage["completion_tokens"] == 1
    assert compact.compression.token_usage["total_tokens"] == 2
    assert result.usage_record is not None
    assert raw_password in rendered_prompt
    assert raw_bearer in rendered_prompt
    assert raw_cookie in rendered_prompt
    assert raw_parameter_token not in rendered_prompt
    assert "<redacted>" in rendered_prompt
    assert "DETERMINISTIC OUTPUT" not in rendered_prompt
    assert result.deterministic_compact_output is not None


@pytest.mark.asyncio
async def test_compress_tool_output_passes_stdout_and_stderr_metadata_to_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Processor metadata should carry both stdout and stderr for failure analysis."""
    captured: Dict[str, Any] = {}

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        captured["metadata"] = dict(metadata)
        return SimpleNamespace(
            summary="Command failed.",
            key_findings=["stderr: permission denied", "stdout: partial output"],
            structured_signals=[{"type": "error_context", "message": "permission denied"}],
            decision_evidence=["permission denied"],
            lossiness_risk="medium",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(
            status="error",
            success=False,
            exit_code=1,
            stdout="partial output",
            stderr="permission denied",
        ),
        artifact_path=None,
        execution_id="exec-meta",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert captured["metadata"]["stdout"] == "partial output"
    assert captured["metadata"]["stderr"] == "permission denied"


@pytest.mark.asyncio
async def test_compress_tool_output_forwards_tool_intent_to_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Builder-supplied per-call intent reaches processor metadata for compression."""
    captured: Dict[str, Any] = {}

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        captured["metadata"] = dict(metadata)
        return SimpleNamespace(
            summary="ok",
            key_findings=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(tool_intent="confirm the host is reachable"),
        artifact_path=None,
        execution_id="exec-intent",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert captured["metadata"]["tool_intent"] == "confirm the host is reachable"


@pytest.mark.asyncio
async def test_compress_tool_output_omits_tool_intent_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent intent must not inject a key, preserving the prompt's 'none' fallback."""
    captured: Dict[str, Any] = {}

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        captured["metadata"] = dict(metadata)
        return SimpleNamespace(
            summary="ok",
            key_findings=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    await compress_tool_output(
        tool_name="shell.exec",
        raw_result=_base_raw_result(),
        artifact_path=None,
        execution_id="exec-no-intent",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert "tool_intent" not in captured["metadata"]


@pytest.mark.asyncio
async def test_compress_tool_output_carries_semantic_envelope_metadata_to_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compressor forwards shared semantic transport fields via processor metadata."""
    captured: Dict[str, Any] = {}

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        captured["metadata"] = dict(metadata)
        return SimpleNamespace(
            summary="ok",
            key_findings=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    await compress_tool_output(
        tool_name="network.nmap_scan",
        raw_result=_base_raw_result(
            metadata={
                "semantic_observations": [{"observation_type": "network.open_port"}],
                "semantic_evidence": [
                    {
                        "type": "diagnostic",
                        "name": "ssh_banner",
                        "value": "OpenSSH_8.2",
                        "detail": {"note": "port_22"},
                    }
                ],
                "capability_family": "network_discovery",
                "semantic_schema_version": "nmap.v1",
            }
        ),
        artifact_path=None,
        execution_id="exec-semantics",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert captured["metadata"]["semantic_observations"] == [
        {"observation_type": "network.open_port"}
    ]
    assert captured["metadata"]["semantic_evidence"] == [
        {
            "type": "diagnostic",
            "name": "ssh_banner",
            "value": "OpenSSH_8.2",
            "detail": {"note": "port_22"},
        }
    ]
    assert captured["metadata"]["capability_family"] == "network_discovery"
    assert captured["metadata"]["semantic_schema_version"] == "nmap.v1"


@pytest.mark.asyncio
async def test_compress_tool_output_carries_wrapped_semantic_envelope_metadata_to_processor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compressor supports wrapped semantic fields under metadata.tool_metadata."""
    captured: Dict[str, Any] = {}

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        captured["metadata"] = dict(metadata)
        return SimpleNamespace(
            summary="ok",
            key_findings=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    await compress_tool_output(
        tool_name="network.nmap_scan",
        raw_result=_base_raw_result(
            metadata={
                "tool_metadata": {
                    "semantic_observations": [{"observation_type": "network.open_port"}],
                    "semantic_evidence": [
                        {
                            "type": "diagnostic",
                            "name": "ssh_banner",
                            "value": "OpenSSH_8.2",
                            "detail": {"note": "port_22"},
                        }
                    ],
                    "capability_family": "network_discovery",
                    "semantic_schema_version": "nmap.v1",
                }
            }
        ),
        artifact_path=None,
        execution_id="exec-semantics-wrapped",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert captured["metadata"]["semantic_observations"] == [
        {"observation_type": "network.open_port"}
    ]
    assert captured["metadata"]["semantic_evidence"] == [
        {
            "type": "diagnostic",
            "name": "ssh_banner",
            "value": "OpenSSH_8.2",
            "detail": {"note": "port_22"},
        }
    ]
    assert captured["metadata"]["capability_family"] == "network_discovery"
    assert captured["metadata"]["semantic_schema_version"] == "nmap.v1"


@pytest.mark.asyncio
async def test_compress_tool_output_merges_split_semantic_envelope_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compressor merges flat and wrapped semantic fields from split envelopes."""
    captured: Dict[str, Any] = {}

    async def _process_output_stub(self, tool_name: str, raw_output: str, metadata: Dict[str, Any]):  # noqa: ANN001
        captured["metadata"] = dict(metadata)
        return SimpleNamespace(
            summary="ok",
            key_findings=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    await compress_tool_output(
        tool_name="network.nmap_scan",
        raw_result=_base_raw_result(
            metadata={
                "semantic_observations": [{"observation_type": "network.open_port"}],
                "semantic_evidence": [
                    {
                        "type": "diagnostic",
                        "name": "ssh_banner",
                        "value": "OpenSSH_8.2",
                        "detail": {"note": "port_22"},
                    }
                ],
                "tool_metadata": {
                    "capability_family": "network_discovery",
                    "semantic_schema_version": "nmap.v1",
                },
            }
        ),
        artifact_path=None,
        execution_id="exec-semantics-split",
        llm_client=SimpleNamespace(model="gpt-4o-mini"),
    )

    assert captured["metadata"]["semantic_observations"] == [
        {"observation_type": "network.open_port"}
    ]
    assert captured["metadata"]["semantic_evidence"] == [
        {
            "type": "diagnostic",
            "name": "ssh_banner",
            "value": "OpenSSH_8.2",
            "detail": {"note": "port_22"},
        }
    ]
    assert captured["metadata"]["capability_family"] == "network_discovery"
    assert captured["metadata"]["semantic_schema_version"] == "nmap.v1"
