"""Unit tests for compact tool-output compression behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Mapping

import pytest

from core.prompts.constants import (
    COMPACT_DECISION_EVIDENCE_MAX_CHARS,
    COMPACT_KEY_FINDINGS_MAX_ITEMS,
    COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS,
    COMPACT_SUMMARY_MAX_CHARS,
)
from agent.graph.compression.compressor import (
    _build_canonical_pentest_fact_projection,
    compress_tool_output,
)
from agent.graph.compression.pentest_facts import CompactFactContext
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from agent.graph.compression.schema import CompactToolOutput, CompressionMetadata
from runtime_shared.semantic.pentest_facts.contracts import CompiledFactSet

HTTP_REQUEST_TOOL_ID = "information_gathering.web_enumeration.http_request"
HYDRA_TOOL_ID = "password_attacks.online_attacks.hydra"
NMAP_TOOL_ID = "information_gathering.network_discovery.nmap"


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


def _open_port_semantic_row() -> Dict[str, Any]:
    return {
        "observation_type": "network.open_port",
        "subject_type": "service.socket",
        "subject_key": "service.socket:192.0.2.20/tcp/443",
        "payload": {"ip": "192.0.2.20", "protocol": "tcp", "port": 443},
    }


def _canonical_projection_raw_result(
    *,
    metadata: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    merged_metadata: Dict[str, Any] = {
        "semantic_schema_version": "pentest-facts.v1",
        "capability_family": "network.discovery",
        "semantic_observations": [_open_port_semantic_row()],
        "semantic_evidence": [
            {
                "type": "diagnostic",
                "name": "service_banner",
                "value": "443/tcp open https",
            }
        ],
    }
    if metadata:
        merged_metadata.update(dict(metadata))
    return _base_raw_result(metadata=merged_metadata)


def test_canonical_projection_helper_passes_only_compiled_facts_and_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical projection receives compiled facts plus explicit context only."""

    captured: Dict[str, Any] = {}

    def _project_stub(
        compiled: CompiledFactSet,
        context: CompactFactContext,
    ) -> SimpleNamespace:
        captured["compiled"] = compiled
        captured["context"] = context
        return SimpleNamespace(
            compact_output=CompactToolOutput(
                tool=context.tool,
                status=context.status,
                success=context.success,
                exit_code=context.exit_code,
                summary=context.compact_summary or "projected summary",
                key_findings=list(context.compact_key_findings),
                structured_signals=[
                    dict(item) for item in context.compact_structured_signals
                ],
                decision_evidence=list(context.compact_decision_evidence),
                artifact_refs=list(context.artifact_refs),
                compression=CompressionMetadata(source="deterministic"),
            )
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.project_compact_facts",
        _project_stub,
    )

    compact = _build_canonical_pentest_fact_projection(
        tool_name="information_gathering.network_discovery.nmap",
        raw_result=_canonical_projection_raw_result(
            metadata={
                "compact_summary": " Canonical override summary. ",
                "compact_key_findings": [" 443/tcp open ", "443/tcp open"],
                "compact_decision_evidence": [" proof line\ncontinues "],
                "structured_signals": [
                    {
                        "type": "service",
                        "port": 443,
                        "service": "https",
                        "extra": "drop",
                    }
                ],
            }
        ),
        artifact_path="/workspace/artifacts/nmap.xml",
        execution_id="exec-canonical",
        status="success",
        success=True,
        exit_code=0,
    )

    assert compact is not None
    assert isinstance(captured["compiled"], CompiledFactSet)
    assert captured["compiled"].accepted_count == 1
    context = captured["context"]
    assert isinstance(context, CompactFactContext)
    assert context.compact_summary == "Canonical override summary."
    assert context.compact_key_findings == ("443/tcp open",)
    assert context.compact_decision_evidence == ("proof line continues",)
    assert context.compact_structured_signals == (
        {"type": "service", "port": 443, "service": "https"},
    )
    assert [ref.to_dict() for ref in context.artifact_refs] == [
        {
            "path": "/workspace/artifacts/nmap.xml",
            "artifact_id": None,
            "execution_id": "exec-canonical",
            "tool_call_id": None,
            "tool_name": None,
            "artifact_kind": None,
            "label": None,
            "relative_path": None,
        }
    ]


def test_canonical_projection_helper_keeps_projector_dto_with_all_generic_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compressor must not replace the projector-owned compact DTO."""

    projected_output = CompactToolOutput(
        tool="information_gathering.network_discovery.nmap",
        status="success",
        success=True,
        exit_code=0,
        summary="Projector-owned summary.",
        key_findings=["Projector-owned finding."],
        structured_signals=[
            {"type": "service", "port": 8443, "service": "projector"}
        ],
        decision_evidence=[
            "Projector-owned metadata evidence.",
            "Projector-owned canonical evidence.",
        ],
        compression=CompressionMetadata(source="deterministic"),
    )

    def _project_stub(
        compiled: CompiledFactSet,
        context: CompactFactContext,
    ) -> SimpleNamespace:
        assert compiled.accepted_count == 1
        assert context.compact_summary == "Override summary."
        assert context.compact_key_findings == ("Override finding.",)
        assert context.compact_structured_signals == (
            {"type": "service", "port": 443, "service": "https"},
        )
        assert context.compact_decision_evidence == ("Override evidence.",)
        return SimpleNamespace(compact_output=projected_output)

    monkeypatch.setattr(
        "agent.graph.compression.compressor.project_compact_facts",
        _project_stub,
    )

    compact = _build_canonical_pentest_fact_projection(
        tool_name="information_gathering.network_discovery.nmap",
        raw_result=_canonical_projection_raw_result(
            metadata={
                "compact_summary": " Override summary. ",
                "compact_key_findings": [" Override finding. "],
                "compact_structured_signals": [
                    {"type": "service", "port": 443, "service": "https"}
                ],
                "compact_decision_evidence": [" Override evidence. "],
            }
        ),
        artifact_path=None,
        execution_id="exec-projector-authority",
        status="success",
        success=True,
        exit_code=0,
    )

    assert compact is projected_output


def test_canonical_projection_helper_ignores_pcap_compact_metadata() -> None:
    """PCAP compact payloads are not deterministic inputs for canonical projection."""

    compact = _build_canonical_pentest_fact_projection(
        tool_name="sniffing_spoofing.network_sniffers.tshark",
        raw_result=_canonical_projection_raw_result(
            metadata={
                "pcap_compact": {
                    "schema_version": "pcap.compact.v1",
                    "hosts": [{"ip": "192.0.2.20"}],
                }
            }
        ),
        artifact_path=None,
        execution_id="exec-pcap-ignore",
        status="success",
        success=True,
        exit_code=0,
    )

    assert compact is not None
    payload = compact.to_dict()
    assert "pcap_compact" not in str(payload)


@pytest.mark.asyncio
async def test_compress_tool_output_merges_partial_metadata_evidence_before_canonical() -> None:
    """Partial metadata evidence override should not suppress canonical projection."""

    result = await compress_tool_output(
        tool_name="information_gathering.network_discovery.nmap",
        raw_result=_canonical_projection_raw_result(
            metadata={
                "compact_decision_evidence": [" metadata evidence first "],
            }
        ),
        artifact_path=None,
        execution_id="exec-partial-evidence",
        llm_client=None,
    )

    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.summary == "Projected 1 service facts."
    assert deterministic.key_findings == [
        "service: network.open_port service.socket:192.0.2.20/tcp/443"
    ]
    assert deterministic.decision_evidence == [
        "metadata evidence first",
        "evidence: diagnostic; service_banner=443/tcp open https; detail={}",
    ]


def test_canonical_projection_helper_invalid_semantics_do_not_emit_secondary() -> None:
    """Invalid canonical input is non-crashing and cannot revive legacy parsing."""

    compact = _build_canonical_pentest_fact_projection(
        tool_name="information_gathering.network_discovery.nmap",
        raw_result=_base_raw_result(
            metadata={
                "semantic_schema_version": "pentest-facts.v1",
                "capability_family": "network.discovery",
                "semantic_observations": [
                    {
                        "observation_type": "network.open_port",
                        "subject_type": "service.socket",
                        "subject_key": "service.socket:192.0.2.20/tcp/443",
                        "payload": "invalid payload",
                    }
                ],
                "semantic_evidence": [{"bad": "row"}],
            }
        ),
        artifact_path=None,
        execution_id="exec-invalid",
        status="success",
        success=True,
        exit_code=0,
    )

    assert compact is None


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
    assert deterministic.decision_evidence == ["Metadata evidence stays first."]
    assert compact.structured_signals == [{"type": "service", "port": 22, "service": "ssh"}]
    assert deterministic.structured_signals == [
        {"type": "service", "port": 443, "service": "https"}
    ]
    assert compact.report_recommendations == []
    assert compact.compression is not None
    assert compact.compression.source == "llm"


@pytest.mark.asyncio
async def test_compress_tool_output_metadata_overrides_replace_adapter_fields_and_lock_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic metadata overrides own the deterministic lane with current bounds."""

    long_evidence = "x" * (COMPACT_DECISION_EVIDENCE_MAX_CHARS + 10)
    long_summary = "s" * (COMPACT_SUMMARY_MAX_CHARS + 10)
    max_key_finding = "k" * COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS

    async def _process_output_stub(
        self: object,
        tool_name: str,
        raw_output: str,
        metadata: Dict[str, Any],
    ) -> SimpleNamespace:
        return SimpleNamespace(
            summary="Processor summary remains primary.",
            key_findings=["Processor finding remains primary."],
            next_actions=[],
            structured_signals=[{"type": "service", "port": 443, "service": "https"}],
            decision_evidence=["Processor evidence remains primary."],
            lossiness_risk="medium",
            analysis_source="llm",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.override_adapter",
        raw_result=_base_raw_result(
            metadata={
                "compact_summary": f" {long_summary} ",
                "compact_key_findings": [
                    " finding-1 ",
                    "finding-2",
                    "finding-3",
                    "finding-4",
                    "finding-5",
                    "finding-6",
                    "finding-2",
                    *[
                        f"finding-extra-{index}"
                        for index in range(COMPACT_KEY_FINDINGS_MAX_ITEMS)
                    ],
                    max_key_finding,
                    "",
                ],
                "compact_structured_signals": [
                    {"type": "service", "port": 22, "service": "ssh"},
                ],
                "compact_decision_evidence": [
                    " metadata first \n line ",
                    "metadata first line",
                    long_evidence,
                ],
                "fs_search_text": {
                    "matches": [
                        {
                            "path": "artifacts/result.txt",
                            "line": 9,
                            "column": 1,
                            "snippet": "service=ssh",
                        }
                    ],
                    "truncated": False,
                },
            },
        ),
        artifact_path=None,
        execution_id="exec-override-bounds",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert result.compact_output.summary == "Processor summary remains primary."
    assert deterministic.summary == long_summary[:COMPACT_SUMMARY_MAX_CHARS]
    assert deterministic.key_findings == [
        "finding-1",
        "finding-2",
        "finding-3",
        "finding-4",
        "finding-5",
        "finding-6",
        *[
            f"finding-extra-{index}"
            for index in range(COMPACT_KEY_FINDINGS_MAX_ITEMS - 6)
        ],
    ]
    assert len(deterministic.key_findings) == COMPACT_KEY_FINDINGS_MAX_ITEMS
    assert (
        sum(len(item) for item in deterministic.key_findings)
        + len(deterministic.key_findings)
        - 1
    ) <= COMPACT_KEY_FINDINGS_TOTAL_MAX_CHARS
    assert max_key_finding not in deterministic.key_findings
    assert deterministic.structured_signals == [
        {"type": "service", "port": 22, "service": "ssh"}
    ]
    assert deterministic.decision_evidence == [
        "metadata first line",
        long_evidence[: COMPACT_DECISION_EVIDENCE_MAX_CHARS - 3] + "...",
    ]
    assert all(
        len(item) <= COMPACT_DECISION_EVIDENCE_MAX_CHARS
        for item in deterministic.decision_evidence
    )


@pytest.mark.asyncio
async def test_compress_tool_output_normalizes_structured_signals_alias_only_in_secondary_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current structured_signals alias is compressor metadata only."""

    captured_metadata: Dict[str, Any] = {}
    alias_signals = [{"type": "unsupported", "value": "drop"}] + [
        {"type": "service", "port": index, "service": "ssh", "extra": "drop"}
        for index in range(30)
    ]

    async def _process_output_stub(
        self: object,
        tool_name: str,
        raw_output: str,
        metadata: Dict[str, Any],
    ) -> SimpleNamespace:
        captured_metadata.update(metadata)
        return SimpleNamespace(
            summary="Processor primary summary.",
            key_findings=[],
            next_actions=[],
            structured_signals=[],
            decision_evidence=[],
            lossiness_risk="low",
            analysis_source="llm",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )

    result = await compress_tool_output(
        tool_name="registry_wiring_tests.structured_alias",
        raw_result=_base_raw_result(
            metadata={
                "compact_summary": "Alias secondary summary.",
                "structured_signals": alias_signals,
            },
        ),
        artifact_path=None,
        execution_id="exec-structured-alias",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert "structured_signals" not in captured_metadata
    assert len(deterministic.structured_signals) == 25
    assert deterministic.structured_signals[0] == {
        "type": "service",
        "port": 0,
        "service": "ssh",
    }
    assert deterministic.structured_signals[-1] == {
        "type": "service",
        "port": 24,
        "service": "ssh",
    }
    assert all("extra" not in signal for signal in deterministic.structured_signals)


def test_compact_tool_output_to_dict_locks_schema_version_and_shape() -> None:
    """CompactToolOutput serialization stays stable for primary and secondary lanes."""

    compact = CompactToolOutput(
        tool="registry_wiring_tests.schema",
        status="success",
        success=True,
        exit_code=0,
        summary="Schema lock summary.",
        key_findings=["finding"],
        structured_signals=[{"type": "service", "port": 443, "service": "https"}],
        decision_evidence=["evidence"],
        lossiness_risk="low",
        compression=CompressionMetadata(source="deterministic"),
    )

    payload = compact.to_dict()

    assert list(payload) == [
        "schema_version",
        "tool",
        "status",
        "success",
        "exit_code",
        "summary",
        "key_findings",
        "errors",
        "report_recommendations",
        "structured_signals",
        "decision_evidence",
        "lossiness_risk",
        "artifact_refs",
        "compression",
    ]
    assert payload == {
        "schema_version": "2.0",
        "tool": "registry_wiring_tests.schema",
        "status": "success",
        "success": True,
        "exit_code": 0,
        "summary": "Schema lock summary.",
        "key_findings": ["finding"],
        "errors": [],
        "report_recommendations": [],
        "structured_signals": [{"type": "service", "port": 443, "service": "https"}],
        "decision_evidence": ["evidence"],
        "lossiness_risk": "low",
        "artifact_refs": [],
        "compression": {
            "source": "deterministic",
            "model": None,
            "token_usage": None,
            "fallback_reason": None,
        },
    }
    assert CompactToolOutput.from_dict(payload).to_dict() == payload


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


@pytest.mark.asyncio
async def test_compress_tool_output_pentest_uses_canonical_projection_not_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pentest-role secondary output comes from canonical projection only."""
    processor_calls: list[str] = []
    captured_metadata: list[Dict[str, Any]] = []

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

    monkeypatch.setattr(
        "agent.graph.compression.compressor.UniversalToolProcessor.process_output",
        _process_output_stub,
    )
    raw_result = _canonical_projection_raw_result(
        metadata={
            "compact_summary": "Canonical secondary summary.",
            "compact_key_findings": ["canonical secondary finding"],
            "compact_decision_evidence": ["canonical secondary evidence"],
        }
    )

    result = await compress_tool_output(
        tool_name=NMAP_TOOL_ID,
        raw_result=raw_result,
        artifact_path="/workspace/artifacts/nmap-complete.xml",
        execution_id="exec-complete-real-tool-fixture",
        llm_client=SimpleNamespace(model="gpt-4.1"),
    )

    compact = result.compact_output

    assert processor_calls == [NMAP_TOOL_ID]
    assert "deterministic_analysis" not in captured_metadata[0]
    assert compact.summary == f"Processor summary for {NMAP_TOOL_ID}."
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
    assert result.compact_output is result.llm_compact_output
    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.summary == "Canonical secondary summary."
    assert deterministic.key_findings == ["canonical secondary finding"]
    assert deterministic.decision_evidence == [
        "canonical secondary evidence",
        "evidence: diagnostic; service_banner=443/tcp open https; detail={}",
    ]


@pytest.mark.asyncio
async def test_http_request_without_canonical_facts_does_not_use_old_adapter() -> None:
    """HTTP request output without canonical facts does not revive old parsing."""

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

    assert compact.summary == "HTTP/1.1 200 OK"
    assert deterministic is None
    assert compact.compression is not None
    assert compact.compression.source == "deterministic"


@pytest.mark.asyncio
async def test_compress_tool_output_utility_catalog_role_uses_processor_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Utility-role tools should use the processor path without registry dispatch."""
    service_access_tool_id = "service_access.ftp_login"
    calls: list[str] = []

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
    """Generic compact metadata remains secondary without adapter execution."""
    calls: list[str] = []
    captured_metadata: Dict[str, Any] = {}

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

    assert calls == ["processor:registry_wiring_tests.complete_adapter"]
    assert "deterministic_analysis" not in captured_metadata
    assert compact.summary == "Processor summary is available."
    assert compact.key_findings == ["Processor finding is available."]
    assert compact.structured_signals == [{"type": "service", "port": 443, "service": "https"}]
    assert compact.decision_evidence == ["Processor evidence is available."]
    deterministic = result.deterministic_compact_output
    assert deterministic is not None
    assert deterministic.summary == "Metadata compact summary wins."
    assert deterministic.key_findings == ["Metadata compact finding wins."]
    assert deterministic.structured_signals == []
    assert deterministic.decision_evidence == ["Metadata compact evidence wins."]
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
    """Pentest fallback-role tools do not use partial deterministic adapters."""
    calls: list[str] = []
    captured_metadata: Dict[str, Any] = {}

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
    assert deterministic is None


@pytest.mark.asyncio
async def test_compress_tool_output_partial_adapter_keeps_llm_output_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM-backed primary output remains authoritative after pentest cutover."""
    calls: list[str] = []

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
    """Missing secondary metadata should keep processor output authoritative."""

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
    assert result.deterministic_compact_output is None


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
