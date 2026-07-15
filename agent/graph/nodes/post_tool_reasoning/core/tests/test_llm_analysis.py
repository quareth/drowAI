"""Tests for capability-agnostic LLM analysis."""

import pytest

from agent.providers.llm.core.exceptions import LLMResponseError, LLMStructuredOutputParseError
from core.llm import LLMTimeoutError
from ..llm_analysis import (
    analyze_tool_result,
    analyze_tool_result_with_retry,
    build_analysis_context,
    MAX_REASONING_TOKENS,
    DEFAULT_TEMPERATURE,
)
from ...models import (
    PostToolReasoningDecisionOutput,
    PostToolReasoningError,
    RetryablePostToolReasoningError,
)
from ..failure_detection import FailureContext


class MockLLMClient:
    """Mock LLMClient for testing."""
    
    def __init__(self, response: str, should_fail: bool = False):
        self.response = response
        self.should_fail = should_fail
        self.call_count = 0
        self.last_system_prompt = None
        self.last_user_prompt = None
        self.last_temperature = None
        self.last_max_tokens = None
        self.last_reasoning_effort = None
        self.call_kwargs = []

    class Response:
        """Minimal chat_with_usage payload shape."""
        
        def __init__(self, content: str, structured_output: dict | None = None):
            self.content = content
            self.structured_output = structured_output
            self.usage = None
    
    async def chat_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 500,
        reasoning_effort: str | None = None,
        structured_output: dict | None = None,
    ):
        """Mock chat_with_usage method."""
        self.call_count += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        self.last_temperature = temperature
        self.last_max_tokens = max_tokens
        self.last_reasoning_effort = reasoning_effort
        self.call_kwargs.append(
            {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "reasoning_effort": reasoning_effort,
                "structured_output": structured_output,
            }
        )
        
        if self.should_fail:
            raise Exception("Mock LLM error")

        return self.Response(self.response, structured_output=structured_output)

    # Backward-compatible chat shim for older tests in this module and elsewhere.
    async def chat(self, *args, **kwargs):
        return self.response


@pytest.mark.asyncio
class TestAnalyzeToolResult:
    """Tests for analyze_tool_result function."""
    
    async def test_analyze_tool_result_success(self):
        """Verify LLM analysis produces valid output."""
        valid_response = """{"next_action": "finalize", "action_reasoning": "Scan complete", "user_goal_achieved": true}"""
        
        mock_client = MockLLMClient(valid_response)
        
        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )
        
        assert isinstance(output, PostToolReasoningDecisionOutput)
        assert output.next_action == "finalize"
        assert output.user_goal_achieved is True
        assert mock_client.call_count == 1
        assert mock_client.last_temperature == DEFAULT_TEMPERATURE
        assert mock_client.last_max_tokens == MAX_REASONING_TOKENS

    async def test_analyze_tool_result_structured_output_is_decision_only(self):
        """Verify structured result can omit observation and still validate."""
        mock_client = MockLLMClient("ignored")

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                "ignored legacy text",
                structured_output={
                    "next_action": "think_more",
                    "action_reasoning": "Need to reason before another call.",
                },
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "think_more"
        assert output.action_reasoning == "Need to reason before another call."

    async def test_analyze_tool_result_does_not_require_delimiter(self):
        """Decision-only structured output should not require ===DECISION=== formatting."""
        mock_client = MockLLMClient("legacy mixed-format placeholder")

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                "legacy text with no delimiter",
                structured_output={
                    "next_action": "call_tool",
                    "action_reasoning": "Proceed with corrected follow-up.",
                    "tool_intent": {"description": "Retry with corrected params"},
                },
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "call_tool"
        assert output.tool_intent is not None
        assert output.tool_intent.description == "Retry with corrected params"

    async def test_analyze_tool_result_falls_back_when_structured_output_is_invalid_type(self):
        """Malformed structured_output should fall back to parsing response content."""
        valid_response = """{"next_action": "finalize", "action_reasoning": "Structured format invalid type fallback", "user_goal_achieved": true}"""
        mock_client = MockLLMClient(valid_response)

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                valid_response,
                structured_output=17,
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "finalize"
        assert output.user_goal_achieved is True

    async def test_analyze_tool_result_enforces_call_tool_has_tool_intent(self):
        """A call_tool decision without tool_intent should be normalized to a safe action."""
        mock_client = MockLLMClient("ignored")

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                "ignored legacy content",
                structured_output={
                    "next_action": "call_tool",
                    "action_reasoning": "Proceed with additional checks",
                },
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "reflect"
        assert output.tool_intent is None

    async def test_analyze_tool_result_rejects_invalid_structured_payload(self):
        """Invalid structured payload should fail fast in decision-only path."""
        mock_client = MockLLMClient("legacy text")

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                "legacy text with no delimiter",
                structured_output={"action_reasoning": "Missing next_action"},
            )

        mock_client.chat_with_usage = chat_with_structured_output

        with pytest.raises(PostToolReasoningError):
            await analyze_tool_result(
                mock_client,
                "system prompt",
                "user prompt",
            )

    async def test_analyze_canonicalizes_legacy_vulnerability_observation_type(self):
        """Legacy vulnerability observation_type aliases should be normalized."""
        mock_client = MockLLMClient("ignored")

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                "ignored",
                structured_output={
                    "next_action": "finalize",
                    "action_reasoning": "Tool output reviewed",
                    "candidate_observations": [
                        {
                            "observation_type": "vulnerability_candidates",
                            "subject_type": "software_version",
                            "subject_key_hint": "PostgreSQL 9.6.0",
                            "assertion_level": "candidate",
                            "confidence": 0.58,
                            "attributes": [{"key": "product", "value": "PostgreSQL"}],
                            "rationale": "Possible CVE matches",
                            "evidence_refs": [
                                {
                                    "source_artifact_id": "artifact-1",
                                    "excerpt": "possible matches",
                                }
                            ],
                            "vulnerability": {
                                "id": "MULTI",
                                "title": "Potential CVE matches",
                                "severity": "unknown",
                            },
                            "vulnerability_confidence": 0.58,
                        }
                    ],
                },
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(mock_client, "system prompt", "user prompt")

        assert output.candidate_observations is not None
        assert len(output.candidate_observations) == 1
        assert (
            output.candidate_observations[0].observation_type
            == "finding.vulnerability_candidate"
        )

    async def test_analyze_drops_invalid_candidate_observation_rows(self):
        """Invalid candidate rows should be dropped instead of failing the decision."""
        mock_client = MockLLMClient("ignored")

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                "ignored",
                structured_output={
                    "next_action": "finalize",
                    "action_reasoning": "Tool output reviewed",
                    "candidate_observations": [
                        {
                            "observation_type": "finding.vulnerability_candidate",
                            "subject_type": "software_version",
                            "subject_key_hint": "PostgreSQL 9.6.0",
                            "assertion_level": "candidate",
                            "confidence": 0.58,
                            "attributes": [{"key": "product", "value": "PostgreSQL"}],
                            "rationale": "Missing evidence refs should invalidate this row",
                            "evidence_refs": [],
                            "vulnerability": {
                                "id": "MULTI",
                                "title": "Potential CVE matches",
                                "severity": "unknown",
                            },
                            "vulnerability_confidence": 0.58,
                        },
                        {
                            "observation_type": "finding.vulnerability_candidate",
                            "subject_type": "software_version",
                            "subject_key_hint": "PostgreSQL 9.6.0",
                            "assertion_level": "candidate",
                            "confidence": 0.58,
                            "attributes": [{"key": "product", "value": "PostgreSQL"}],
                            "rationale": "Valid row should remain",
                            "evidence_refs": [
                                {
                                    "source_artifact_id": "artifact-2",
                                    "excerpt": "possible matches",
                                }
                            ],
                            "vulnerability": {
                                "id": "MULTI",
                                "title": "Potential CVE matches",
                                "severity": "unknown",
                            },
                            "vulnerability_confidence": 0.58,
                        },
                    ],
                },
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(mock_client, "system prompt", "user prompt")

        assert output.candidate_observations is not None
        assert len(output.candidate_observations) == 1
        assert output.candidate_observations[0].rationale == "Valid row should remain"

    async def test_analyze_uses_structured_output_first(self):
        """Structured payload should bypass content parsing."""
        mock_client = MockLLMClient("ignored")
        expected_payload = {
            "next_action": "finalize",
            "action_reasoning": "Structured response is authoritative",
            "user_goal_achieved": True,
        }

        async def chat_with_structured_output(*args, **kwargs):
            return MockLLMClient.Response(
                "not-json-body",
                structured_output=expected_payload,
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "finalize"
        assert output.action_reasoning == "Structured response is authoritative"

    async def test_analyze_coerces_null_todo_progress_to_empty_list(self):
        """Provider may emit nullable todo_progress; parser should normalize it."""
        mock_client = MockLLMClient("ignored")

        async def chat_with_structured_output(*_args, **_kwargs):
            return MockLLMClient.Response(
                "ignored",
                structured_output={
                    "next_action": "finalize",
                    "action_reasoning": "No todo delta this turn",
                    "user_goal_achieved": True,
                    "todo_progress": None,
                },
            )

        mock_client.chat_with_usage = chat_with_structured_output

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "finalize"
        assert output.todo_progress == []
    
    async def test_analyze_handles_failure_context(self):
        """Verify failure context passed correctly (for logging)."""
        valid_response = """{"next_action": "reflect", "action_reasoning": "Network issue", "failure_detected": true, "failure_category": "network_error", "retry_suggested": false, "user_goal_achieved": false}"""
        
        mock_client = MockLLMClient(valid_response)
        failure_ctx = FailureContext(
            success_flag=False,
            status="error",
            exit_code=1,
            stdout="",
            stderr="connection refused",
            summary="",
            key_findings=[],
        )
        
        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
            failure_context=failure_ctx,
        )
        
        assert output.failure_detected is True
        assert output.failure_category == "network_error"
    
    async def test_analyze_llm_call_failure(self):
        """Verify LLM call failures raise PostToolReasoningError."""
        mock_client = MockLLMClient("", should_fail=True)
        
        with pytest.raises(PostToolReasoningError) as exc_info:
            await analyze_tool_result(
                mock_client,
                "system prompt",
                "user prompt",
            )
        
        assert "LLM call failed" in str(exc_info.value)

    async def test_analyze_recovers_from_structured_parse_failure_with_plain_text_fallback(self):
        """Malformed provider structured JSON should retry once without structured mode."""
        mock_client = MockLLMClient("")
        call_kwargs = []

        async def chat_with_fallback(*_args, **kwargs):
            call_kwargs.append(kwargs)
            if len(call_kwargs) == 1:
                raise LLMStructuredOutputParseError(
                    "structured parse failed",
                    provider="OpenAI",
                    schema_name="post_tool_decision",
                    parse_reason="json_decode_error",
                    raw_content='{"next_action":"call_tool"',
                    diagnostics={"response_id": "resp_123", "status": "incomplete"},
                )
            return MockLLMClient.Response(
                '{"next_action":"finalize","action_reasoning":"Recovered after plain-text fallback","user_goal_achieved":true}',
                structured_output=None,
            )

        mock_client.chat_with_usage = chat_with_fallback

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "finalize"
        assert output.user_goal_achieved is True
        assert len(call_kwargs) == 2
        assert call_kwargs[0]["structured_output"] is not None
        assert "structured_output" not in call_kwargs[1]

    async def test_analyze_truncated_plain_text_fallback_recovers(self):
        """Truncated fallback JSON should recover when required fields are present."""
        mock_client = MockLLMClient("")

        async def chat_with_truncated_fallback(*_args, **kwargs):
            if kwargs.get("structured_output") is not None:
                raise LLMStructuredOutputParseError(
                    "structured parse failed",
                    provider="OpenAI",
                    schema_name="post_tool_decision",
                    parse_reason="json_decode_error",
                    raw_content='{"next_action":"finalize"',
                    diagnostics={"response_id": "resp_456", "status": "incomplete"},
                )
            return MockLLMClient.Response(
                '{"next_action":"finalize","action_reasoning":"Recovered from truncated fallback',
                structured_output=None,
            )

        mock_client.chat_with_usage = chat_with_truncated_fallback

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )
        assert output.next_action == "finalize"
        assert "Recovered from truncated fallback" in output.action_reasoning

    async def test_analyze_raises_retryable_error_when_fallback_parse_is_exhausted(self):
        """If the plain-text fallback still cannot be parsed, raise retryable checkpoint error."""
        mock_client = MockLLMClient("")

        async def chat_with_unrecoverable_fallback(*_args, **kwargs):
            if kwargs.get("structured_output") is not None:
                raise LLMStructuredOutputParseError(
                    "structured parse failed",
                    provider="OpenAI",
                    schema_name="post_tool_decision",
                    parse_reason="json_decode_error",
                    raw_content='{"next_action":"finalize"',
                    diagnostics={"response_id": "resp_789", "status": "incomplete"},
                )
            return MockLLMClient.Response("this is not parseable", structured_output=None)

        mock_client.chat_with_usage = chat_with_unrecoverable_fallback

        with pytest.raises(RetryablePostToolReasoningError) as exc_info:
            await analyze_tool_result(
                mock_client,
                "system prompt",
                "user prompt",
            )

        assert exc_info.value.error_code == "provider_structured_output_parse"
        assert exc_info.value.retryable is True

    async def test_analyze_with_retry_recovers_after_retryable_contract_failure(self):
        """Retry wrapper should recover silently when second attempt succeeds."""
        mock_client = MockLLMClient("")
        attempt_counter = {"structured_calls": 0}

        async def chat_retry_then_success(*_args, **kwargs):
            if kwargs.get("structured_output") is not None:
                attempt_counter["structured_calls"] += 1
                if attempt_counter["structured_calls"] == 1:
                    raise LLMStructuredOutputParseError(
                        "structured parse failed",
                        provider="OpenAI",
                        schema_name="post_tool_decision",
                        parse_reason="json_decode_error",
                        raw_content='{"next_action":"finalize"',
                        diagnostics={"response_id": "resp_retry_once"},
                    )
                return MockLLMClient.Response(
                    "",
                    structured_output={
                        "next_action": "finalize",
                        "action_reasoning": "Recovered on retry",
                        "user_goal_achieved": True,
                    },
                )
            raise RuntimeError("fallback call failed")

        mock_client.chat_with_usage = chat_retry_then_success

        output = await analyze_tool_result_with_retry(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "finalize"
        assert output.user_goal_achieved is True
        assert attempt_counter["structured_calls"] == 2

    async def test_analyze_with_retry_recovers_after_llm_timeout(self):
        """Retry wrapper should reuse the same retry path for timeout failures."""
        mock_client = MockLLMClient("")
        attempt_counter = {"calls": 0}

        async def chat_timeout_then_success(*_args, **kwargs):
            attempt_counter["calls"] += 1
            if attempt_counter["calls"] == 1:
                raise LLMTimeoutError(
                    task_id=17,
                    component="POST_TOOL_REASONING",
                    operation="post_tool_decision_llm_call",
                    timeout_sec=120,
                    outcome="post_tool_decision_timeout",
                )
            return MockLLMClient.Response(
                "",
                structured_output={
                    "next_action": "finalize",
                    "action_reasoning": "Recovered after timeout",
                    "user_goal_achieved": True,
                },
            )

        mock_client.chat_with_usage = chat_timeout_then_success

        output = await analyze_tool_result_with_retry(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "finalize"
        assert output.user_goal_achieved is True
        assert attempt_counter["calls"] == 2

    async def test_analyze_with_retry_raises_once_when_retries_exhausted(self):
        """Retry wrapper should re-raise final retryable error after bounded attempts."""
        mock_client = MockLLMClient("")

        async def chat_always_fails(*_args, **kwargs):
            if kwargs.get("structured_output") is not None:
                raise LLMStructuredOutputParseError(
                    "structured parse failed",
                    provider="OpenAI",
                    schema_name="post_tool_decision",
                    parse_reason="json_decode_error",
                    raw_content='{"next_action":"finalize"',
                    diagnostics={"response_id": "resp_exhaust"},
                )
            raise RuntimeError("fallback call failed")

        mock_client.chat_with_usage = chat_always_fails

        with pytest.raises(RetryablePostToolReasoningError) as exc_info:
            await analyze_tool_result_with_retry(
                mock_client,
                "system prompt",
                "user prompt",
            )

        assert exc_info.value.error_code == "provider_structured_output_parse"
        assert exc_info.value.retryable is True

    async def test_analyze_recovers_from_empty_provider_content_with_plain_text_fallback(self):
        """Empty provider content should retry once without structured mode."""
        mock_client = MockLLMClient("")
        call_kwargs = []

        async def chat_with_empty_then_fallback(*_args, **kwargs):
            call_kwargs.append(kwargs)
            if kwargs.get("structured_output") is not None:
                raise LLMResponseError(
                    "OpenAI Responses API returned empty content",
                    provider="OpenAI",
                )
            return MockLLMClient.Response(
                '{"next_action":"finalize","action_reasoning":"Recovered from empty content","user_goal_achieved":true}',
                structured_output=None,
            )

        mock_client.chat_with_usage = chat_with_empty_then_fallback

        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )

        assert output.next_action == "finalize"
        assert output.user_goal_achieved is True
        assert len(call_kwargs) == 2
        assert call_kwargs[0]["structured_output"] is not None
        assert "structured_output" not in call_kwargs[1]

    async def test_analyze_raises_retryable_error_when_empty_content_fallback_fails(self):
        """If provider content is empty and fallback also fails, error must remain retryable."""
        mock_client = MockLLMClient("")

        async def chat_with_empty_and_failed_fallback(*_args, **kwargs):
            if kwargs.get("structured_output") is not None:
                raise LLMResponseError(
                    "OpenAI Responses API returned empty content",
                    provider="OpenAI",
                )
            raise RuntimeError("fallback call failed")

        mock_client.chat_with_usage = chat_with_empty_and_failed_fallback

        with pytest.raises(RetryablePostToolReasoningError) as exc_info:
            await analyze_tool_result(
                mock_client,
                "system prompt",
                "user prompt",
            )

        assert exc_info.value.error_code == "provider_structured_output_parse"
        assert exc_info.value.retryable is True
    
    async def test_analyze_parsing_failure(self):
        """Verify parsing failures raise PostToolReasoningError."""
        invalid_response = "This is not valid JSON format"
        mock_client = MockLLMClient(invalid_response)
        
        with pytest.raises(PostToolReasoningError):
            await analyze_tool_result(
                mock_client,
                "system prompt",
                "user prompt",
            )
    
    async def test_analyze_uses_correct_parameters(self):
        """Verify correct temperature and max_tokens used."""
        valid_response = """{"next_action": "finalize", "action_reasoning": "Analysis complete, goal achieved", "user_goal_achieved": true}"""
        
        mock_client = MockLLMClient(valid_response)
        
        await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )
        
        assert mock_client.last_temperature == DEFAULT_TEMPERATURE
        assert mock_client.last_max_tokens == MAX_REASONING_TOKENS
    
    async def test_analyze_with_retry_suggestion(self):
        """Verify retry suggestions parsed correctly."""
        valid_response = """{"next_action": "call_tool", "action_reasoning": "Retry with different params", "tool_intent": {"description": "Retry scan", "target": "127.0.0.1", "focus": null}, "failure_detected": true, "failure_category": "invalid_params", "retry_suggested": true, "user_goal_achieved": false}"""
        
        mock_client = MockLLMClient(valid_response)
        
        output = await analyze_tool_result(
            mock_client,
            "system prompt",
            "user prompt",
        )
        
        assert output.next_action == "call_tool"
        assert output.retry_suggested is True
        assert output.tool_intent is not None
        assert output.tool_intent.description == "Retry scan"
    
    async def test_analyze_empty_response(self):
        """Verify empty response raises error."""
        mock_client = MockLLMClient("")
        
        with pytest.raises(PostToolReasoningError):
            await analyze_tool_result(
                mock_client,
                "system prompt",
                "user prompt",
            )
    
    async def test_analyze_malformed_json(self):
        """Verify malformed JSON raises error."""
        malformed_response = """{this is not valid json}"""
        
        mock_client = MockLLMClient(malformed_response)
        
        with pytest.raises(PostToolReasoningError):
            await analyze_tool_result(
                mock_client,
                "system prompt",
                "user prompt",
            )


class TestBuildAnalysisContext:
    """Tests for build_analysis_context function."""
    
    def test_build_context_no_failure(self):
        """Verify context built correctly when no failure."""
        context = build_analysis_context(
            failure_detected=False,
            failure_category=None,
            retry_count=0,
            max_retries=4,
        )
        
        assert context["failure_detected"] is False
        assert context["failure_category"] is None
        assert context["retry_count"] == 0
        assert context["max_retries"] == 4
        assert context["retry_budget_remaining"] == 4
    
    def test_build_context_with_failure(self):
        """Verify context built correctly with failure."""
        context = build_analysis_context(
            failure_detected=True,
            failure_category="network_error",
            retry_count=2,
            max_retries=4,
        )
        
        assert context["failure_detected"] is True
        assert context["failure_category"] == "network_error"
        assert context["retry_count"] == 2
        assert context["retry_budget_remaining"] == 2
    
    def test_build_context_retry_budget_exhausted(self):
        """Verify retry budget calculation when exhausted."""
        context = build_analysis_context(
            failure_detected=True,
            failure_category="timeout",
            retry_count=4,
            max_retries=4,
        )
        
        assert context["retry_budget_remaining"] == 0
    
    def test_build_context_retry_budget_negative(self):
        """Verify retry budget never goes negative."""
        context = build_analysis_context(
            failure_detected=True,
            failure_category="unknown",
            retry_count=5,
            max_retries=4,
        )
        
        assert context["retry_budget_remaining"] == 0
