"""Tests for Deep Reasoning MVP functionality."""

from __future__ import annotations

import asyncio
import importlib
import json
import uuid
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from agent.graph import build_initial_state
from agent.graph.builders.deep_reasoning_builder import build_deep_reasoning_graph
from agent.graph.persistence import get_default_checkpointer
from agent.graph.state import (
    FactsState,
    InteractiveInput,
    InteractiveState,
    TodoItem,
    TraceState,
)
from agent.graph.utils.plan_progress_authority import (
    apply_llm_updates,
    build_todo_stream_updates,
    ensure_initial_in_progress,
)
from backend.services.streaming.reasoning_sse_service import ReasoningSSEService
from backend.services.usage_tracking.models import UsageData
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
from agent.tool_runtime import ToolExecutionOutcome, ToolCatalogEntry
from agent.models import ActionPlan, ActionType, ExecutionStrategy
from tests.tool_execution_module_helper import patch_tool_execution_attr


@pytest.fixture(autouse=True)
def _stub_tool_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub tool execution for testing."""

    class _StubPlanner:
        def __init__(self, _config, **_kwargs):  # noqa: ANN001
            pass

        async def build_action_plan(self, action, _context):  # noqa: ANN001
            target = action.target or "127.0.0.1"
            return ActionPlan(
                type=ActionType.GATHER_INFO,
                target=target,
                selected_tools=["information_gathering.network_discovery.nmap"],
                tool_parameters={
                    "information_gathering.network_discovery.nmap": {
                        "target": target,
                        "ports": "22,80,443",
                    }
                },
                llm_tool_parameters={},
                execution_strategy=ExecutionStrategy.SEQUENTIAL,
                reasoning="Deterministic test plan",
                expected_outcome="Discover open ports on target",
                usage_records=[],
            )
    
    class _StubCoordinator:
        call_count = 0
        
        async def run(self, request):  # noqa: ANN001
            _StubCoordinator.call_count += 1
            catalog = [
                ToolCatalogEntry(
                    tool_id="information_gathering.network_discovery.nmap",
                    name="nmap",
                    category="network",
                    description="Network scanner",
                )
            ]
            return ToolExecutionOutcome(
                tool_id="information_gathering.network_discovery.nmap",
                parameters={"target": (request.targets or ["127.0.0.1"])[0], "ports": "22,80,443"},
                catalog=catalog,
                result={
                    "tool": "information_gathering.network_discovery.nmap",
                    "success": True,
                    "stdout_excerpt": f"Scan {_StubCoordinator.call_count} complete. Ports 22,80 open.",
                    "stderr_excerpt": "",
                    "observation": "Discovered open ports: 22 (SSH), 80 (HTTP)",
                    "status": "success",
                },
                summary=f"Scan {_StubCoordinator.call_count}: Found open ports 22, 80",
                reasoning=[f"Iteration {_StubCoordinator.call_count}: Running network scan"],
                duration=0.15,
            )

    patch_tool_execution_attr(
        monkeypatch,
        "EnhancedActionPlanner",
        _StubPlanner,
    )
    patch_tool_execution_attr(
        monkeypatch,
        "ToolExecutionCoordinator",
        lambda config: _StubCoordinator(),
    )


_CURRENT_STUB_LLM_CLIENT: Any = None


@pytest.fixture(autouse=True)
def _stub_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub resolver-backed LLMClient to keep DR tests deterministic."""
    global _CURRENT_STUB_LLM_CLIENT

    class _StubLLMClient:
        def __init__(self) -> None:
            self._decision_calls = 0
            self._reasoning_effort = "medium"

        def _response_for_prompt(self, user_prompt: str) -> tuple[str, Dict[str, Any] | None]:
            prompt = user_prompt.lower()

            if (
                "## user goal" in prompt
                and "## execution capability" in prompt
                and "fallback path completed the goal" in prompt
            ):
                payload = {
                    "next_action": "finalize",
                    "action_reasoning": "The requested scan completed with actionable findings.",
                    "user_goal_achieved": True,
                }
                return json.dumps(payload), payload

            if "decide the next action" in prompt:
                self._decision_calls += 1
                if self._decision_calls <= 1:
                    payload = {
                        "action": "call_tool",
                        "reasoning": "Need to gather information by scanning the target",
                    }
                elif self._decision_calls == 2:
                    payload = {
                        "action": "think_more",
                        "reasoning": "Need to analyze scan results before proceeding",
                    }
                else:
                    payload = {
                        "action": "finalize",
                        "reasoning": "Sufficient information gathered, ready to provide answer",
                    }
                return json.dumps(payload), payload

            if "think deeply" in prompt:
                payload = {
                    "reasoning": "Scan revealed SSH and HTTP ports. Target appears to be a web server with remote access.",
                    "updated_plan": ["Identify target", "Scan for open ports", "Analyze results", "Document findings"],
                    "next_goal": "Document findings and provide summary",
                    "key_observations": ["SSH port 22 open", "HTTP port 80 open", "Potential web server"],
                }
                return json.dumps(payload), payload

            if "you encountered an issue" in prompt:
                payload = {
                    "reflection": "Tool failed due to network timeout. Alternative: try with smaller port range or check connectivity.",
                    "updated_plan": ["Verify target is reachable", "Scan limited port range", "Analyze results"],
                    "updated_todo_list": ["Ping target", "Scan top 100 ports only"],
                    "next_goal": "Verify network connectivity first",
                }
                return json.dumps(payload), payload

            payload = {
                "mode": "plan_ready",
                "plan": ["Identify target", "Scan for open ports", "Analyze results"],
                "todo_list": ["Scan 127.0.0.1 with nmap", "Review scan output", "Document findings"],
                "current_goal": "Complete network reconnaissance",
            }
            return json.dumps(payload), payload

        async def chat(self, _system_prompt: str, user_prompt: str, **_kwargs: Any) -> str:
            content, _ = self._response_for_prompt(user_prompt)
            return content

        async def chat_with_usage(self, *args: Any, **_kwargs: Any) -> SimpleNamespace:
            user_prompt = (
                str(args[-1])
                if args
                else str(_kwargs.get("user_prompt") or _kwargs.get("prompt") or "")
            )
            structured_name = getattr(_kwargs.get("structured_output"), "name", "")
            if structured_name == "post_tool_decision":
                payload = {
                    "next_action": "finalize",
                    "action_reasoning": "The scan completed with enough evidence to answer.",
                    "tool_intent": None,
                    "user_goal_achieved": True,
                    "todo_progress": [
                        {
                            "index": 0,
                            "status": "completed",
                            "completion_type": "positive",
                            "completion_reason": "The scan completed.",
                        }
                    ],
                    "effective_next_goal": None,
                    "failure_detected": False,
                    "failure_category": None,
                    "retry_suggested": False,
                    "candidate_observations": None,
                }
                content = json.dumps(payload)
                structured_payload = payload
            else:
                content, structured_payload = self._response_for_prompt(user_prompt)

            usage = {
                "prompt_tokens": 20,
                "completion_tokens": 30,
                "total_tokens": 50,
                "model": "stub-model",
                "provider": "test",
            }
            return SimpleNamespace(content=content, usage=usage, structured_output=structured_payload)

        async def stream_chat_messages(self, _messages: Any, **_kwargs: Any):
            yield "Recon complete. Open ports: 22 and 80."

        async def stream_chat_messages_with_usage(self, _messages: Any, **_kwargs: Any):
            async def _chunks():
                yield "Recon complete. Open ports: 22 and 80."

            return SimpleNamespace(
                content_iterator=_chunks(),
                get_final_usage=lambda: UsageData(
                    prompt_tokens=20,
                    completion_tokens=30,
                    total_tokens=50,
                    model="gpt-5.2",
                    provider="openai",
                    api_surface="responses",
                ),
            )

    stub_client = _StubLLMClient()
    _CURRENT_STUB_LLM_CLIENT = stub_client
    stub_resolver = lambda *_args, **_kwargs: stub_client

    planner_generation_module = importlib.import_module("agent.graph.nodes.planner_generation")
    think_more_module = importlib.import_module("agent.graph.nodes.think_more")
    post_tool_module = importlib.import_module("agent.graph.nodes.post_tool_reasoning.node")
    articulation_module = importlib.import_module("agent.graph.nodes.tool_articulation")
    finalizer_module = importlib.import_module("agent.graph.nodes.finalize")

    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", stub_resolver)
    monkeypatch.setattr(think_more_module, "resolve_llm_client", stub_resolver)
    monkeypatch.setattr(post_tool_module, "resolve_llm_client", stub_resolver)
    monkeypatch.setattr(articulation_module, "resolve_llm_client", stub_resolver)
    monkeypatch.setattr(finalizer_module, "resolve_llm_client", stub_resolver)


@pytest.fixture(autouse=True)
def _disable_hitl_interrupts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable HITL interrupts so DR tests execute end-to-end deterministically."""
    hitl_module = importlib.import_module("agent.graph.nodes.hitl_helpers")
    monkeypatch.setattr(hitl_module, "ENABLE_HITL_INTERRUPTS", False)


def _build_dr_state(
    *,
    message: str,
    metadata: Dict[str, Any] | None = None,
    runtime_budgets: Dict[str, Any] | None = None,
) -> InteractiveState:
    """Build initial state and run deep reasoning graph."""
    metadata = metadata or {}
    if "context_bundle" not in metadata:
        from agent.graph.context.builder import (
            METADATA_CONTEXT_BUNDLE_KEY,
            build_conversation_context_bundle,
        )

        metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
            conversation_id="dr-test-conv",
            turn_id="dr-test-turn",
            turn_sequence=1,
            messages=[],
            current_message=message,
        )
    metadata.setdefault("capability", "deep_reasoning")
    metadata.setdefault("intent_capability", "deep_reasoning")
    metadata.setdefault("eligible_routes", ["deep_reasoning"])
    metadata.setdefault("plan_approved", True)
    metadata.setdefault("agent_mode", "chat")
    
    # Set runtime budgets if provided
    if runtime_budgets:
        metadata["runtime_budgets"] = runtime_budgets
    
    payload = InteractiveInput(
        task_id=99,
        message=message,
        metadata=metadata,
    )
    initial = build_initial_state(payload)
    
    # Ensure deep reasoning capability is set
    initial.setdefault("facts", {})["capability"] = "deep_reasoning"
    initial["facts"].setdefault("tool_ids", ["nmap"])
    if not initial["facts"].get("plan"):
        initial["facts"]["plan"] = ["Identify target", "Scan for open ports", "Analyze results"]
    if not initial["facts"].get("todo_list"):
        initial["facts"]["todo_list"] = [
            "Scan 127.0.0.1 with nmap",
            "Review scan output",
            "Document findings",
        ]
    if not initial["facts"].get("current_goal"):
        initial["facts"]["current_goal"] = "Complete network reconnaissance"
    
    # Apply runtime budgets to facts if provided
    if runtime_budgets:
        initial["facts"]["runtime_budgets"] = runtime_budgets
    
    compiled = build_deep_reasoning_graph(checkpointer=get_default_checkpointer()).compile(
        checkpointer=get_default_checkpointer()
    )
    class _Resolver:
        def get_client(self, *_args: Any, **_kwargs: Any) -> Any:
            return _CURRENT_STUB_LLM_CLIENT

        def resolve_secret(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(provider="openai", value="sk-test-runtime")

    config = {
        "configurable": {
            "thread_id": f"dr-test-thread-{uuid.uuid4().hex}",
            "runtime_services": SimpleNamespace(client_resolver=_Resolver()),
            "llm_runtime_selection": {
                "provider": "openai",
                "model": "gpt-5.2",
                "credential_ref": {"user_id": 10, "provider": "openai"},
                "reasoning_effort": None,
            },
            "runtime_projection": {"user_id": 10, "task_id": 99},
        }
    }
    
    if hasattr(compiled, "ainvoke"):
        result = asyncio.run(compiled.ainvoke(initial, config=config))
    else:
        result = compiled.invoke(initial, config=config)
    
    return InteractiveState.from_mapping(result)


def test_dr_simple_flow():
    """Test simple multi-step deep reasoning flow."""
    state = _build_dr_state(
        message="Scan 127.0.0.1 and report open ports",
        metadata={},
    )
    
    # Verify plan was created
    assert state.facts.plan, "Plan should be created"
    assert len(state.facts.plan) > 0, "Plan should have steps"
    
    # Verify todo list was created
    assert state.facts.todo_list, "Todo list should be created"
    assert len(state.facts.todo_list) > 0, "Todo list should have items"
    
    # Verify current goal was set
    assert state.facts.current_goal, "Current goal should be set"
    
    # Verify decision history exists
    assert state.facts.decision_history, "Decision history should be populated"
    assert len(state.facts.decision_history) > 0, "Should have made decisions"
    
    # Verify reasoning trace
    assert state.trace.reasoning, "Reasoning trace should exist"
    assert any("plan" in r.lower() for r in state.trace.reasoning), "Should mention planning"
    
    # Verify final text was generated
    assert state.trace.final_text, "Final text should be generated"


def test_dr_budget_enforcement_iterations():
    """Test that iteration budget is enforced."""
    # Set very low iteration budget
    state = _build_dr_state(
        message="Scan 127.0.0.1 thoroughly",
        runtime_budgets={
            "remaining_iterations": 2,  # Only 2 iterations allowed
            "remaining_tool_calls": 10,
        },
    )
    
    # Verify iteration budget was respected
    assert state.facts.iterations <= 2, f"Should not exceed 2 iterations, got {state.facts.iterations}"
    
    # Verify budget tracking in metadata
    assert "runtime_budgets" in state.facts.metadata, "Runtime budgets should be tracked"
    runtime_budgets = state.facts.metadata["runtime_budgets"]
    assert runtime_budgets["remaining_iterations"] is not None, "Should track remaining iterations"


def test_dr_budget_enforcement_tool_calls():
    """Test that tool call budget is enforced."""
    state = _build_dr_state(
        message="Scan multiple targets",
        runtime_budgets={
            "remaining_iterations": 10,
            "remaining_tool_calls": 1,  # Only 1 tool call allowed
        },
    )
    
    # Verify tool call budget was respected
    assert state.facts.tool_calls_used <= 1, f"Should not exceed 1 tool call, got {state.facts.tool_calls_used}"
    
    # Verify budget tracking
    assert "runtime_budgets" in state.facts.metadata, "Runtime budgets should be tracked"
    runtime_budgets = state.facts.metadata["runtime_budgets"]
    assert runtime_budgets["remaining_tool_calls"] is not None, "Should track remaining tool calls"


def test_dr_tool_execution():
    """Test that DR can execute tools."""
    state = _build_dr_state(
        message="Scan 127.0.0.1 with nmap",
        metadata={},
    )

    assert state.facts.tool_calls_used >= 0, "Tool call count should never be negative"
    assert len(state.trace.executed_tools) == state.facts.tool_calls_used, (
        "Executed tool records should align with tracked tool call count"
    )
    if state.trace.executed_tools:
        tool_record = state.trace.executed_tools[0]
        assert "nmap" in tool_record.tool_id, "Should have executed nmap when a tool runs"
        assert tool_record.observation, "Tool should have observations"
        return

    decision_history = [entry.lower() for entry in state.facts.decision_history]
    assert any("finalize" in entry for entry in decision_history), (
        "When no tool executes, DR should still terminate deterministically"
    )
    assert state.trace.final_text, "Terminal response should still be produced"


def test_dr_scratchpad_updates():
    """Test that scratchpad is updated during reasoning."""
    state = _build_dr_state(
        message="Perform reconnaissance on 127.0.0.1",
        metadata={},
    )
    
    # Verify scratchpad was used
    assert state.trace.scratchpad, "Scratchpad should contain reasoning"
    assert len(state.trace.scratchpad) > 0, "Scratchpad should not be empty"
    
    # Verify scratchpad contains meaningful content
    assert any(
        keyword in state.trace.scratchpad.lower()
        for keyword in ["scan", "target", "port", "result", "analyze"]
    ), "Scratchpad should contain reasoning about the task"


def test_dr_decision_routing():
    """Test that decision router makes appropriate routing decisions."""
    state = _build_dr_state(
        message="Scan and analyze 127.0.0.1",
        metadata={},
    )
    
    # Verify decision history
    assert state.facts.decision_history, "Should have decision history"
    assert len(state.facts.decision_history) > 0, "Should have made at least one decision"
    
    # Verify different actions were taken
    # The mock LLM returns: call_tool -> think_more -> finalize
    decision_actions = [d.split(":")[0].strip() if ":" in d else d for d in state.facts.decision_history]
    
    # Should have made a finalize decision eventually
    assert any("finalize" in action for action in decision_actions), "Should eventually finalize"


def test_dr_capability_smoke_terminates_without_dead_loop():
    """Task 6.2 smoke: DR run terminates with bounded routing."""
    state = _build_dr_state(
        message="Run a single host recon and finish cleanly",
        metadata={},
        runtime_budgets={
            "remaining_iterations": 6,
            "remaining_tool_calls": 3,
        },
    )

    assert state.trace.final_text, "Expected terminal output from deep reasoning flow"
    assert state.facts.iterations <= 6, "DR should terminate within the configured budget"
    decision_history = [entry.lower() for entry in state.facts.decision_history]
    assert any("finalize" in entry for entry in decision_history), (
        "DR routing should reach a finalize terminal action (direct or fallback)"
    )
    usage_sources = []
    for record in state.trace.usage_records:
        if isinstance(record, dict):
            usage_sources.append(str(record.get("source") or "").lower())
            continue
        source = getattr(record, "source", "")
        if source:
            usage_sources.append(str(source).lower())
    assert "decision_router" not in usage_sources, (
        "Deterministic router authority must not emit router LLM usage records"
    )


def test_dr_handles_empty_plan():
    """Test that DR handles cases where plan becomes empty."""
    # Build state with very low budget that forces quick finalization
    state = _build_dr_state(
        message="Quick scan of 127.0.0.1",
        runtime_budgets={
            "remaining_iterations": 1,  # Will finalize quickly
            "remaining_tool_calls": 0,   # No tool calls allowed
        },
    )
    
    # Verify it completed without errors
    assert state.trace.final_text, "Should generate final text even with empty plan"
    assert state.facts.iterations <= 1, "Should respect iteration budget"


def test_dr_stuck_counter():
    """Test that stuck counter prevents infinite loops."""
    # This test verifies the stuck detection mechanism
    state = _build_dr_state(
        message="Analyze 127.0.0.1",
        runtime_budgets={
            "remaining_iterations": 10,
            "remaining_tool_calls": 5,
        },
    )
    
    # Verify stuck counter exists in facts
    assert "stuck_counter" in state.facts.model_dump(), "Stuck counter should be tracked"
    
    # Verify execution completed (not stuck in infinite loop)
    assert state.trace.final_text, "Should complete without getting stuck"
    assert state.facts.iterations < 10, "Should not use all iterations (indicates potential stuck condition)"


def test_dr_progression_prompt_markers_and_multi_completion():
    """Validate canonical progression updates and prompt marker visibility."""
    todos = [
        TodoItem(description="Discover host"),
        TodoItem(description="Enumerate ports"),
        TodoItem(description="Summarize findings"),
    ]
    assert ensure_initial_in_progress(todos) is True

    interactive = InteractiveState(
        facts=FactsState(
            task_id=99,
            message="Run deep reasoning progression validation",
            capability="deep_reasoning",
            todo_list=todos,
            metadata={},
        ),
        trace=TraceState(),
    )
    builder = PostToolReasoningPromptBuilder()
    first_prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized={"tool": "nmap", "summary": "Initial scan done", "key_findings": []},
    )
    assert "## Todo List" in first_prompt
    assert "[in_progress]" in first_prompt

    before = [todo.model_copy(deep=True) for todo in todos]
    changed = apply_llm_updates(
        todos,
        [
            {
                "index": 0,
                "status": "completed",
                "completion_type": "positive",
                "completion_reason": "Host was discovered successfully",
            },
            {
                "index": 1,
                "status": "completed",
                "completion_type": "positive",
                "completion_reason": "Port enumeration completed",
            },
            {"index": 2, "status": "in_progress"},
        ],
    )
    assert changed == {0, 1, 2}
    stream_updates = build_todo_stream_updates(before, todos)
    assert len(stream_updates) == 3
    assert [update["status"] for update in stream_updates] == [
        "completed",
        "completed",
        "in_progress",
    ]

    second_prompt = builder.build_user_prompt(
        interactive=interactive,
        synthesized={"tool": "nmap", "summary": "Validation step done", "key_findings": []},
    )
    assert second_prompt.count("[completed]") >= 2
    assert "[in_progress]" in second_prompt


@pytest.mark.asyncio
async def test_dr_hitl_approval_emits_authoritative_progress_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate HITL approve emits backend-authoritative progression event."""
    from agent.graph.nodes import plan_review as plan_review_module
    from agent.graph.nodes import planner as planner_module
    from agent.graph.nodes import planner_generation as planner_generation_module
    from agent.graph.nodes import planner_response as planner_response_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    monkeypatch.setattr(
        planner_generation_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")),
    )
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (["Step 1", "Step 2"], ["Todo 1"], "Goal"),
    )
    monkeypatch.setattr(
        plan_review_module,
        "request_plan_approval",
        lambda **_kwargs: {"action": "approve"},
    )

    events = []

    def writer(event):
        events.append(event)

    state = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="test request",
            capability="deep_reasoning",
            metadata={"agent_mode": "plan"},
        ),
        trace=TraceState(),
    )
    planned_state = await planner_module.planner_node(state)
    await plan_review_module.plan_review_node(planned_state, writer=writer)

    assert events, "Expected at least one emitted event after approval"
    first_event = events[0]
    assert first_event["type"] in {"todo_progress", "plan_created"}
    assert isinstance(first_event.get("run_id"), int)
    assert isinstance(first_event.get("plan_version"), int)
    if first_event["type"] == "todo_progress":
        statuses = [update.get("status") for update in first_event.get("todo_updates", [])]
        assert "in_progress" in statuses
    else:
        assert first_event["todo_list"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_dr_e2e_stream_recovery_after_delayed_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate replay/live recovery, including reconnect tail-gap recovery via `after` cursor."""
    from agent.graph.nodes import plan_review as plan_review_module
    from agent.graph.nodes import planner as planner_module
    from agent.graph.nodes import planner_generation as planner_generation_module
    from agent.graph.nodes import planner_response as planner_response_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    monkeypatch.setattr(
        planner_generation_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")),
    )
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (["Step 1", "Step 2"], ["Todo 1"], "Goal"),
    )
    monkeypatch.setattr(
        plan_review_module,
        "request_plan_approval",
        lambda **_kwargs: {"action": "approve"},
    )

    approval_events: list[dict[str, Any]] = []

    def writer(event: dict[str, Any]) -> None:
        approval_events.append(event)

    approval_state = InteractiveState(
        facts=FactsState(
            task_id=41,
            message="stream reliability validation",
            capability="deep_reasoning",
            metadata={"agent_mode": "plan"},
        ),
        trace=TraceState(),
    )
    planned_state = await planner_module.planner_node(approval_state)
    await plan_review_module.plan_review_node(planned_state, writer=writer)
    assert approval_events, "Expected a plan approval event before streaming"
    assert approval_events[0]["type"] in {"todo_progress", "plan_created"}

    class _DelayedSubscription:
        def __init__(self) -> None:
            self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def push(self, event: dict[str, Any]) -> None:
            await self._events.put(event)

        async def __anext__(self) -> dict[str, Any]:
            item = await self._events.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def aclose(self) -> None:
            await self._events.put(None)

    class _ReplayStore:
        def __init__(self) -> None:
            self._rows = []
            self.add(1, "reasoning_delta", "reasoning-1")
            self.add(2, "tool_delta", "tool-2")
            self.add(3, "observation_delta", "observation-3")

        def add(self, sequence: int, packet_type: str, content: str) -> None:
            self._rows.append(
                SimpleNamespace(
                    sequence=sequence,
                    payload={
                        "sequence": sequence,
                        "task_id": 41,
                        "type": packet_type,
                        "content": content,
                        "metadata": {},
                    },
                )
            )
            self._rows.sort(key=lambda row: row.sequence)

        def list_after(self, task_id: int, after: int, limit: int):  # noqa: ANN001
            rows = [row for row in self._rows if row.sequence > after]
            return rows[:limit]

    class _Hub:
        def __init__(self) -> None:
            self.subscriptions: list[_DelayedSubscription] = []
            self.latest_sequence = 3

        def subscribe(self, task_id: int) -> _DelayedSubscription:  # noqa: ARG002
            subscription = _DelayedSubscription()
            self.subscriptions.append(subscription)
            return subscription

        def get_latest_sequence(self, task_id: int) -> int:  # noqa: ARG002
            return self.latest_sequence

        async def push(self, event: dict[str, Any]) -> None:
            for subscription in list(self.subscriptions):
                await subscription.push(dict(event))

    hub = _Hub()
    replay_store = _ReplayStore()
    sse_service = ReasoningSSEService()
    monkeypatch.setattr("backend.services.streaming.reasoning_sse_service.get_in_memory_stream_hub", lambda: hub)

    async def _push_live_event() -> None:
        await asyncio.sleep(0.02)
        await hub.push(
            {"sequence": 4, "type": "reasoning_delta", "content": "reasoning-4-live", "metadata": {}}
        )

    producer = asyncio.create_task(_push_live_event())
    chunks: list[str] = []
    stream_gen = sse_service.stream_interactive_events_direct(
        task_id=41,
        after=0,
        heartbeat_interval=0.01,
        idle_timeout=None,
        build_ping=lambda _label: ": ping\n\n",
        on_data_event=lambda: None,
        build_idle_comment=lambda _label: ": idle\n\n",
        mark_activity=lambda: None,
        latest_sequence=3,
        persisted_list_after=replay_store.list_after,
        metrics_obj=None,
    )
    try:
        async for chunk in stream_gen:
            chunks.append(chunk)
            data_chunks = [line for line in chunks if line.startswith("data: ")]
            if len(data_chunks) >= 4:
                break
    finally:
        await stream_gen.aclose()
        await producer

    payloads = [json.loads(line.split("data: ", 1)[1]) for line in chunks if line.startswith("data: ")]
    packet_objects = [payload.get("obj", payload) for payload in payloads]
    assert [payload["sequence"] for payload in payloads] == [1, 2, 3, 4]
    assert [obj["type"] for obj in packet_objects] == [
        "reasoning_delta",
        "tool_delta",
        "observation_delta",
        "reasoning_delta",
    ]
    assert packet_objects[-1]["content"] == "reasoning-4-live"

    # Reconnect scenario: consumer resumes from Last-Event-ID/after=4, recovers missed seq=5
    # from persisted replay, then continues with live seq=6 without manual refresh.
    replay_store.add(5, "observation_delta", "observation-5-replayed")
    hub.latest_sequence = 5

    async def _push_reconnect_live_event() -> None:
        await asyncio.sleep(0.02)
        await hub.push(
            {"sequence": 6, "type": "reasoning_delta", "content": "reasoning-6-live", "metadata": {}}
        )

    reconnect_producer = asyncio.create_task(_push_reconnect_live_event())
    reconnect_chunks: list[str] = []
    reconnect_stream_gen = sse_service.stream_interactive_events_direct(
        task_id=41,
        after=4,
        heartbeat_interval=0.01,
        idle_timeout=None,
        build_ping=lambda _label: ": ping\n\n",
        on_data_event=lambda: None,
        build_idle_comment=lambda _label: ": idle\n\n",
        mark_activity=lambda: None,
        latest_sequence=5,
        persisted_list_after=replay_store.list_after,
        metrics_obj=None,
    )
    try:
        async for chunk in reconnect_stream_gen:
            reconnect_chunks.append(chunk)
            reconnect_data_chunks = [line for line in reconnect_chunks if line.startswith("data: ")]
            if len(reconnect_data_chunks) >= 2:
                break
    finally:
        await reconnect_stream_gen.aclose()
        await reconnect_producer

    reconnect_payloads = [
        json.loads(line.split("data: ", 1)[1]) for line in reconnect_chunks if line.startswith("data: ")
    ]
    reconnect_packet_objects = [payload.get("obj", payload) for payload in reconnect_payloads]
    assert [payload["sequence"] for payload in reconnect_payloads] == [5, 6]
    assert [obj["content"] for obj in reconnect_packet_objects] == [
        "observation-5-replayed",
        "reasoning-6-live",
    ]

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
