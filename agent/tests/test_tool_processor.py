"""Tests for universal tool-output processing and deterministic fallback."""

import os
import sys
import json
import asyncio
import hashlib
import inspect
import logging
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

try:
    from agent.context.tool_processor import UniversalToolProcessor, ProcessedOutput
    from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
    from agent.semantic.enrichment import validate_semantic_evidence_entries
    from agent.semantic.evidence_vocabulary import (
        SemanticEvidenceType,
        get_evidence_per_type_limit,
    )
except Exception:
    from context.tool_processor import UniversalToolProcessor, ProcessedOutput
    from providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
    from semantic.enrichment import validate_semantic_evidence_entries
    from semantic.evidence_vocabulary import (
        SemanticEvidenceType,
        get_evidence_per_type_limit,
    )


class DummyLLM:
    model = "test-model"

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "summary": "ok",
                    "key_findings": ["f1"],
                    "structured_signals": [],
                    "decision_evidence": [],
                    "lossiness_risk": "low",
                }
            ),
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


class PromptCaptureLLM:
    model = "test-model"

    def __init__(self, structured_output: dict) -> None:
        self.structured_output = structured_output
        self.last_prompt: str | None = None

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs):
        self.last_prompt = user_prompt
        return SimpleNamespace(
            content="",
            structured_output=self.structured_output,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )


NON_SEMANTIC_BASELINE_CASES = (
    pytest.param(
        {
            "case_id": "curl_json",
            "tool_name": "curl",
            "raw_output": (
                '{"service":"api","status":"ok","version":"1.2.3",'
                '"region":"us-east-1","features":["auth","billing"]}'
            ),
            # Phase A baseline hash captured on 2026-04-21 with true v3 parity for empty semantic inputs.
            "expected_prompt_sha256": "fc7d55fbc72a195d30fd6f774ec56fb415f538a95fd02609484481999187800f",
            "expected_prompt_len": 8222,
        },
        id="curl-json-output",
    ),
    pytest.param(
        {
            "case_id": "dig_text",
            "tool_name": "dig",
            "raw_output": "\n".join(
                [
                    "; <<>> DiG 9.18.1 <<>> example.com A",
                    ";; global options: +cmd",
                    ";; Got answer:",
                    ";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 4242",
                    ";; flags: qr rd ra; QUERY: 1, ANSWER: 45, AUTHORITY: 0, ADDITIONAL: 1",
                    ";; QUESTION SECTION:",
                    ";example.com.\t\tIN\tA",
                    ";; ANSWER SECTION:",
                ]
                + [
                    f"example.com.\t300\tIN\tA\t93.184.216.{i % 255}"
                    for i in range(1, 46)
                ]
                + [
                    ";; Query time: 12 msec",
                    ";; SERVER: 8.8.8.8#53(8.8.8.8)",
                    ";; WHEN: Tue Apr 21 10:10:10 UTC 2026",
                    ";; MSG SIZE  rcvd: 848",
                ]
            ),
            # Phase A baseline hash captured on 2026-04-21 with true v3 parity for empty semantic inputs.
            "expected_prompt_sha256": "de47b64d91ab40139bbac12d0aa3f2d8788d5d0833eea7955837c0c492c899be",
            "expected_prompt_len": 10108,
        },
        id="dig-command-output",
    ),
    pytest.param(
        {
            "case_id": "shell_text",
            "tool_name": "shell.exec",
            "raw_output": "\n".join(
                [f"package-{i:02d}: installed" for i in range(1, 51)]
            ),
            # Phase A baseline hash captured on 2026-04-21 with true v3 parity for empty semantic inputs.
            "expected_prompt_sha256": "727bfe9485de572ade5a8ba6c8d8638542009b9ca03dce64d34a47a1adc43fd7",
            "expected_prompt_len": 9227,
        },
        id="plain-shell-output",
    ),
)


@pytest.mark.parametrize("case", NON_SEMANTIC_BASELINE_CASES)
def test_non_semantic_tool_prompt_frozen(case):
    _assert_non_semantic_prompt_baseline_case(case)


def _assert_non_semantic_prompt_baseline_case(case: dict) -> None:
    llm = PromptCaptureLLM(
        structured_output={
            "summary": "baseline summary",
            "key_findings": ["baseline finding"],
            "structured_signals": [{"type": "baseline", "source": "phase0"}],
            "decision_evidence": ["phase0-evidence"],
            "lossiness_risk": "low",
        }
    )
    proc = UniversalToolProcessor(llm_client=llm, logger=logging.getLogger(f"test.tool_processor.baseline.{case['case_id']}"))

    result = asyncio.run(proc.process_output(case["tool_name"], case["raw_output"]))

    assert llm.last_prompt is not None
    assert len(llm.last_prompt) == case["expected_prompt_len"]
    assert (
        hashlib.sha256(llm.last_prompt.encode("utf-8")).hexdigest()
        == case["expected_prompt_sha256"]
    )

    assert result.summary == "baseline summary"
    assert result.key_findings == ["baseline finding"]
    assert result.structured_signals == []
    assert result.decision_evidence == ["phase0-evidence"]
    assert result.lossiness_risk == "low"
    assert result.analysis_source == "llm"


def test_non_semantic_prompt_invariant_holds():
    for case in NON_SEMANTIC_BASELINE_CASES:
        _assert_non_semantic_prompt_baseline_case(case.values[0])


def test_empty_semantic_inputs_prompt_matches_baseline():
    llm = PromptCaptureLLM(
        structured_output={
            "summary": "ok",
            "key_findings": ["f1"],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
        }
    )
    proc = UniversalToolProcessor(llm_client=llm, logger=logging.getLogger("test.tool_processor.empty_semantics"))
    long_output = "\n".join(f"line-{index}" for index in range(60))

    asyncio.run(proc.process_output("shell.exec", long_output))
    success_baseline = llm.last_prompt
    assert success_baseline is not None

    asyncio.run(
        proc.process_output(
            "shell.exec",
            long_output,
            metadata={"semantic_observations": [], "semantic_evidence": []},
        )
    )
    assert llm.last_prompt == success_baseline

    asyncio.run(
        proc.process_output(
            "shell.exec",
            long_output,
            metadata={"status": "error", "stderr": "permission denied"},
        )
    )
    failure_baseline = llm.last_prompt
    assert failure_baseline is not None

    asyncio.run(
        proc.process_output(
            "shell.exec",
            long_output,
            metadata={
                "status": "error",
                "stderr": "permission denied",
                "semantic_observations": [],
                "semantic_evidence": [],
            },
        )
    )
    assert llm.last_prompt == failure_baseline


def test_evidence_block_renders_grouped_by_type():
    llm = PromptCaptureLLM(
        structured_output={
            "summary": "grouped",
            "key_findings": ["f1"],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
        }
    )
    proc = UniversalToolProcessor(llm_client=llm, logger=logging.getLogger("test.tool_processor.grouped_evidence"))
    valid_evidence, _ = validate_semantic_evidence_entries(
        [
            {
                "type": SemanticEvidenceType.RESULT_SUMMARY.value,
                "name": "results_count",
                "value": 4,
            },
            {
                "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
                "name": "threads",
                "value": 40,
            },
        ]
    )

    asyncio.run(
        proc.process_output(
            "shell.exec",
            "\n".join(f"line-{index}" for index in range(80)),
            metadata={
                "semantic_observations": [{"observation_type": "web.path_discovered", "path": "/admin"}],
                "semantic_evidence": valid_evidence,
            },
        )
    )

    assert llm.last_prompt is not None
    assert "SEMANTIC EVIDENCE HANDLING" in llm.last_prompt
    assert '"observation_type":"web.path_discovered"' in llm.last_prompt
    execution_index = llm.last_prompt.index('"execution_parameter":[')
    result_index = llm.last_prompt.index('"result_summary":[')
    assert execution_index < result_index


def test_evidence_block_bounded_per_type():
    llm = PromptCaptureLLM(
        structured_output={
            "summary": "bounded",
            "key_findings": ["f1"],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
        }
    )
    proc = UniversalToolProcessor(llm_client=llm, logger=logging.getLogger("test.tool_processor.bounded_evidence"))
    execution_parameter_limit = get_evidence_per_type_limit(
        SemanticEvidenceType.EXECUTION_PARAMETER
    )
    over_limit_entries = [
        {
            "type": SemanticEvidenceType.EXECUTION_PARAMETER.value,
            "name": f"threads_{index}",
            "value": index,
        }
        for index in range(execution_parameter_limit + 2)
    ]
    valid_evidence, dropped = validate_semantic_evidence_entries(over_limit_entries)
    assert len(valid_evidence) == execution_parameter_limit
    assert len(dropped) == 2

    asyncio.run(
        proc.process_output(
            "shell.exec",
            "\n".join(f"line-{index}" for index in range(80)),
            metadata={"semantic_evidence": valid_evidence},
        )
    )

    assert llm.last_prompt is not None
    assert llm.last_prompt.count('"type":"execution_parameter"') == execution_parameter_limit
    assert f'"name":"threads_{execution_parameter_limit}"' not in llm.last_prompt


def test_unknown_evidence_type_never_reaches_prompt():
    llm = PromptCaptureLLM(
        structured_output={
            "summary": "unknown",
            "key_findings": ["f1"],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
        }
    )
    proc = UniversalToolProcessor(llm_client=llm, logger=logging.getLogger("test.tool_processor.unknown_evidence"))

    asyncio.run(
        proc.process_output(
            "shell.exec",
            "\n".join(f"line-{index}" for index in range(80)),
            metadata={
                "semantic_evidence": [
                    {"type": "not_in_vocab", "name": "foo", "value": "bar"}
                ]
            },
        )
    )

    assert llm.last_prompt is not None
    assert "not_in_vocab" not in llm.last_prompt
    assert "SEMANTIC EVIDENCE HANDLING" not in llm.last_prompt


def test_processor_has_no_tool_name_branching():
    source = inspect.getsource(UniversalToolProcessor._build_prompt)
    assert "tool_name ==" not in source
    assert '"ffuf" in' not in source
    assert "'ffuf' in" not in source
    assert '"nmap" in' not in source
    assert "'nmap' in" not in source


def test_process_output_success():
    proc = UniversalToolProcessor(llm_client=DummyLLM(), logger=logging.getLogger("test.tool_processor.success"))
    result = asyncio.run(proc.process_output("information_gathering.nmap", "{\"a\":1}"))
    assert isinstance(result, ProcessedOutput)
    assert result.summary == "ok"
    assert result.key_findings == ["f1"]


def test_process_output_fallback(monkeypatch):
    class FailingLLM:
        model = "test-model"

        async def chat_with_usage(self, *a, **k):
            raise RuntimeError("fail")

    proc = UniversalToolProcessor(llm_client=FailingLLM(), logger=logging.getLogger("test.tool_processor.fallback"))
    output = "port 80 open\nport 22 open"
    result = asyncio.run(proc.process_output("nmap", output))
    assert result.summary.startswith("port 80")
    assert result.key_findings
    assert result.structured_signals == []
    assert result.decision_evidence == []
    assert result.lossiness_risk == "low"


def test_process_output_propagates_provider_refusal():
    """A structured provider refusal must not become deterministic success."""

    refusal = LLMRefusalError(
        "declined",
        outcome=LLMRefusalOutcome(
            provider="openai",
            model="gpt-4o-mini",
            category="content_filter",
        ),
    )

    class RefusingLLM:
        model = "gpt-4o-mini"

        async def chat_with_usage(self, *args, **kwargs):
            raise refusal

    processor = UniversalToolProcessor(llm_client=RefusingLLM())

    with pytest.raises(LLMRefusalError) as exc_info:
        asyncio.run(
            processor.process_output(
                "shell.exec",
                "line\n" * 100,
            )
        )

    assert exc_info.value is refusal


def test_process_output_failure_fallback_emits_error_context():
    class FailingLLM:
        model = "test-model"

        async def chat_with_usage(self, *a, **k):
            raise RuntimeError("fail")

    proc = UniversalToolProcessor(llm_client=FailingLLM(), logger=logging.getLogger("test.tool_processor.error"))
    result = asyncio.run(
        proc.process_output(
            "shell.exec",
            "permission denied\ncannot open file",
            metadata={"status": "error", "stderr": "permission denied\ncannot open file"},
        )
    )

    assert result.summary.startswith("Command failed:")
    assert result.structured_signals == [
        {"type": "error_context", "message": "permission denied"}
    ]
    assert result.decision_evidence == ["permission denied"]
    assert result.lossiness_risk == "medium"


def test_input_truncation():
    class EchoLLM:
        model = "test-model"

        async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs):
            marker = "Tool output to analyze:"
            assert marker in user_prompt
            sampled_output = user_prompt.split(marker, 1)[1].strip()
            assert len(sampled_output) <= 10000
            assert "HEAD_MARKER" in sampled_output
            assert "MIDDLE_MARKER" in sampled_output
            assert "TAIL_MARKER" in sampled_output
            return SimpleNamespace(
                content=json.dumps({"summary": "done", "key_findings": []}),
                structured_output={
                    "summary": "done",
                    "key_findings": [],
                    "structured_signals": [],
                    "decision_evidence": [],
                    "lossiness_risk": "medium",
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

    long_text = "HEAD_MARKER\n" + ("A" * 5200) + "MIDDLE_MARKER\n" + ("B" * 5200) + "TAIL_MARKER\n"
    proc = UniversalToolProcessor(llm_client=EchoLLM())
    result = asyncio.run(proc.process_output("tool", long_text))
    assert result.summary == "done"
    assert result.token_count > 0


def test_short_command_output_bypasses_llm():
    class TrackingLLM:
        model = "test-model"

        def __init__(self) -> None:
            self.called = False

        async def chat_with_usage(self, *a, **k):
            self.called = True
            return SimpleNamespace(
                content=json.dumps({"summary": "should-not-be-used", "key_findings": ["x"]}),
                structured_output={
                    "summary": "should-not-be-used",
                    "key_findings": ["x"],
                    "structured_signals": [],
                    "decision_evidence": [],
                    "lossiness_risk": "low",
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

    llm = TrackingLLM()
    proc = UniversalToolProcessor(llm_client=llm)
    result = asyncio.run(proc.process_output("shell.exec", "file_a\nfile_b\nfile_c\n"))

    assert llm.called is False
    assert result.analysis_source == "deterministic"
    assert result.analysis_reason == "llm_threshold_bypass"
    assert result.summary.startswith("file_a")


def test_llm_bypass_caps_default_to_low_values():
    """Defaults restore pre-48e14d50 caps; env vars widen the raw-pass window."""
    assert UniversalToolProcessor._LLM_BYPASS_MAX_CHARS == 1200
    assert UniversalToolProcessor._LLM_BYPASS_MAX_LINES == 40


def test_output_above_default_bypass_cap_uses_llm():
    """An output between the old and the raised caps must go through the LLM."""

    class TrackingLLM:
        model = "test-model"

        def __init__(self) -> None:
            self.called = False

        async def chat_with_usage(self, *a, **k):
            self.called = True
            return SimpleNamespace(
                content="not-json",
                structured_output={
                    "summary": "llm summary",
                    "key_findings": [],
                    "structured_signals": [],
                    "decision_evidence": [],
                    "lossiness_risk": "low",
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

    llm = TrackingLLM()
    proc = UniversalToolProcessor(llm_client=llm)
    output = "\n".join(f"line-{index}: payload" for index in range(60))
    assert len(output) <= 3000

    result = asyncio.run(proc.process_output("shell.exec", output))

    assert llm.called is True
    assert result.analysis_source == "llm"


def test_short_command_output_preserves_all_line_findings():
    proc = UniversalToolProcessor(llm_client=None)
    lines = [f"target-{index}" for index in range(8)]
    result = asyncio.run(proc.process_output("shell.exec", "\n".join(lines)))

    assert result.analysis_source == "deterministic"
    assert result.key_findings == lines


def test_process_output_prefers_structured_payload_when_available():
    class StructuredLLM:
        model = "test-model"

        async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs):
            return SimpleNamespace(
                content="not-json",
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                structured_output={
                    "summary": "structured summary",
                    "key_findings": ["finding-a", "finding-b"],
                    "structured_signals": [{"type": "service", "port": 443}],
                    "decision_evidence": ["443/tcp open"],
                    "lossiness_risk": "low",
                },
            )

    proc = UniversalToolProcessor(llm_client=StructuredLLM(), logger=logging.getLogger("test.tool_processor.structured"))
    result = asyncio.run(proc.process_output("filesystem.read_file", "raw tool output"))
    assert result.summary == "structured summary"
    assert result.key_findings == ["finding-a", "finding-b"]
    assert result.structured_signals == [{"type": "service", "port": 443}]
    assert result.decision_evidence == ["443/tcp open"]
    assert result.lossiness_risk == "low"


def test_process_output_accepts_wrapped_semantic_inputs_without_behavior_drift():
    class TrackingLLM:
        model = "test-model"

        def __init__(self) -> None:
            self.called = False

        async def chat_with_usage(self, *a, **k):
            self.called = True
            return SimpleNamespace(
                content=json.dumps({"summary": "should-not-be-used", "key_findings": ["x"]}),
                structured_output={
                    "summary": "should-not-be-used",
                    "key_findings": ["x"],
                    "structured_signals": [],
                    "decision_evidence": [],
                    "lossiness_risk": "low",
                },
                usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            )

    llm = TrackingLLM()
    proc = UniversalToolProcessor(llm_client=llm)
    result = asyncio.run(
        proc.process_output(
            "shell.exec",
            "file_a\nfile_b\nfile_c\n",
            metadata={
                "tool_metadata": {
                    "semantic_observations": [{"observation_type": "network.open_port"}],
                    "semantic_evidence": [
                        {
                            "type": "diagnostic",
                            "name": "ssh_banner",
                            "value": "OpenSSH_8.2",
                        }
                    ],
                }
            },
        )
    )

    assert llm.called is False
    assert result.analysis_source == "deterministic"
    assert result.analysis_reason == "llm_threshold_bypass"
    assert result.summary.startswith("file_a")


def test_failure_prompt_path_receives_semantic_context_without_gating():
    llm = PromptCaptureLLM(
        structured_output={
            "summary": "failure summary",
            "key_findings": ["failure finding"],
            "structured_signals": [{"type": "error_context", "message": "permission denied"}],
            "decision_evidence": ["permission denied"],
            "lossiness_risk": "medium",
        }
    )
    proc = UniversalToolProcessor(
        llm_client=llm,
        logger=logging.getLogger("test.tool_processor.failure_prompt_semantics"),
    )

    result = asyncio.run(
        proc.process_output(
            "shell.exec",
            "permission denied",
            metadata={
                "status": "ERROR",
                "semantic_observations": [{"observation_type": "web.path_discovered", "path": "/admin"}],
                "semantic_evidence": [
                    {
                        "type": "result_summary",
                        "name": "results_count",
                        "value": 0,
                    }
                ],
            },
        )
    )

    assert llm.last_prompt is not None
    assert "Tool output to analyze (contains errors):" in llm.last_prompt
    assert '"observation_type":"web.path_discovered"' in llm.last_prompt
    assert (
        '"result_summary":[{"detail":{},"name":"results_count","type":"result_summary","value":0}]'
        in llm.last_prompt
    )
    assert result.summary == "failure summary"


def test_process_output_timeout_logs_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    class TimeoutLLM:
        model = "test-model"

        async def chat_with_usage(self, *a, **k):
            await asyncio.sleep(0.05)
            return SimpleNamespace(content="", usage=None, structured_output=None)

    monkeypatch.setattr("agent.context.tool_processor.LLM_TIMEOUT_TOOL_OUTPUT_COMPRESSOR_SEC", 0.01)

    proc = UniversalToolProcessor(
        llm_client=TimeoutLLM(),
        logger=logging.getLogger("test.tool_processor.timeout"),
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(proc.process_output("nmap", "port 80 open\n" * 120))

    assert result.analysis_source == "deterministic"
    assert result.summary.startswith("port 80")
    assert "TIMEOUT | Task n/a | TOOL_OUTPUT_COMPRESSOR | tool_output_processing_llm_call" in caplog.text
