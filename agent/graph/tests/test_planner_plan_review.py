"""Tests for planner parsing, plan-review HITL, and planner event emission."""

import pytest

from agent.graph.nodes import plan_review as plan_review_module
from agent.graph.nodes import hitl_helpers as hitl_helpers_module
from agent.graph.nodes import planner_generation as planner_generation_module
from agent.graph.nodes import planner_response as planner_response_module
from agent.graph.state import FactsState, InteractiveState, TraceState
from core.prompts.constants import build_planning_prompt


def _build_state(agent_mode: str) -> InteractiveState:
    facts = FactsState(
        task_id=1,
        message="test request",
        capability="deep_reasoning",
    )
    facts.metadata = {"agent_mode": agent_mode}
    if agent_mode == "plan":
        facts.metadata.update(
            {
                "plan_mode": True,
                "plan_review_required": True,
            }
        )
    return InteractiveState(facts=facts, trace=TraceState())

def _todo_texts(todo_list) -> list[str]:
    return [
        (
            item.description
            if hasattr(item, "description")
            else (
                str(item.get("description") or item.get("text"))
                if isinstance(item, dict)
                else str(item)
            )
        )
        for item in (todo_list or [])
    ]


class _FakeLLMResponse:
    def __init__(self, content: str, structured_output: dict | None = None):
        self.content = content
        self.usage = {}
        self.structured_output = structured_output


class _FakeLLMClient:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    async def chat_with_usage(self, *_args, **_kwargs):
        self.calls += 1
        return _FakeLLMResponse(self._content)


class _FakeSequentialLLMClient:
    def __init__(self, contents: list[str]):
        self._contents = list(contents)
        self.calls = 0

    async def chat_with_usage(self, *_args, **_kwargs):
        self.calls += 1
        if self._contents:
            return _FakeLLMResponse(self._contents.pop(0))
        return _FakeLLMResponse("{}")


class _FakeStructuredLLMClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.calls = 0

    async def chat_with_usage(self, *_args, **_kwargs):
        self.calls += 1
        return _FakeLLMResponse("this is not json", structured_output=self._payload)


class _FailingLLMClient:
    def __init__(self, exc: Exception):
        self._exc = exc
        self.calls = 0

    async def chat_with_usage(self, *_args, **_kwargs):
        self.calls += 1
        raise self._exc


def test_build_planning_prompt_includes_clarify_contract_policy() -> None:
    prompt = build_planning_prompt(
        targets_str="10.0.0.1",
        network_discovery_section="",
        tools_constraint="",
        scope_constraints="",
    )
    assert '"mode": "plan_ready" | "clarify_required"' in prompt
    assert "hard blockers" in prompt
    assert "Ask at most 1-2 blocker questions" in prompt
    assert 'input_type: "select"' in prompt
    assert "Do NOT add administrative/compliance preconditions" in prompt


def test_parse_planning_response_falls_back_when_plan_missing(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (["Fallback step 1", "Fallback step 2"], ["Todo 1"], "Fallback goal"),
    )
    response = """
    {
      "mode": "clarify_required",
      "plan": [],
      "todo_list": [],
      "first_goal": "",
      "clarify_request": {
        "required_blockers": [
          {
            "slot": "target",
            "question": "Which host should be scanned?",
            "input_type": "select",
            "options": ["10.0.0.1", "10.0.0.2"]
          }
        ]
      }
    }
    """
    plan, todo_list, first_goal = planner_response_module.parse_planning_response(
        response,
        "scan host",
        ["10.0.0.1"],
    )
    assert plan == ["Fallback step 1", "Fallback step 2"]
    assert todo_list == ["Todo 1"]
    assert first_goal == "Fallback goal"


@pytest.mark.asyncio
async def test_planner_persists_clarify_decision_before_interrupt(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    llm_client = _FakeLLMClient(
        """
        {
          "mode": "clarify_required",
          "plan": [],
          "todo_list": [],
          "first_goal": "",
            "clarify_request": {
              "required_blockers": [
              {
                "slot": "target",
                "question": "Which host should be scanned?",
                "input_type": "select",
                "options": ["10.0.0.1", "10.0.0.2"]
              }
            ]
          }
        }
        """
    )
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: llm_client)

    state = _build_state("full_access")
    result = await planner_module.planner_node(state)

    metadata = result["facts"]["metadata"]
    assert llm_client.calls == 1
    assert metadata["planner_mode"] == "clarify_required"
    assert metadata["pending_clarify_request"]["required_blockers"][0]["slot"] == "target"
    assert metadata["pending_clarify_request"]["required_blockers"][0]["input_type"] == "select"
    assert metadata["pending_clarify_request"]["required_blockers"][0]["options"] == ["10.0.0.1", "10.0.0.2"]
    assert metadata["clarified_context"] == {}
    assert "target" in metadata["asked_slots"]


@pytest.mark.asyncio
async def test_planner_resume_waits_for_answers_without_llm_recall(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    def _raise_if_called(*_args, **_kwargs):
        raise AssertionError("LLM should not be called while clarify answers are missing")

    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", _raise_if_called)

    state = _build_state("full_access")
    state.facts.metadata.update(
        {
            "pending_clarify_request": {
                "required_blockers": [
                    {
                        "slot": "target",
                        "question": "Which host should be scanned?",
                        "input_type": "select",
                        "options": ["10.0.0.1", "10.0.0.2"],
                    }
                ]
            },
            "clarified_context": {},
        }
    )
    result = await planner_module.planner_node(state)
    metadata = result["facts"]["metadata"]

    assert metadata["planner_mode"] == "clarify_required"
    assert metadata["pending_clarify_request"]["required_blockers"][0]["slot"] == "target"


@pytest.mark.asyncio
async def test_planner_resume_consumes_answers_and_continues_to_plan_ready(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    llm_client = _FakeLLMClient(
        """
        {
          "mode": "plan_ready",
          "plan": ["Step 1: Scan target", "Step 2: Review findings"],
          "todo_list": ["Scan target", "Review findings"],
          "first_goal": "Scan target"
        }
        """
    )
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: llm_client)

    state = _build_state("full_access")
    state.facts.metadata.update(
        {
            "pending_clarify_request": {
                "required_blockers": [
                    {
                        "slot": "target",
                        "question": "Which host should be scanned?",
                        "input_type": "select",
                        "options": ["10.0.0.1", "10.0.0.2"],
                    }
                ]
            },
            "clarified_context": {"target": "10.0.0.1"},
        }
    )
    result = await planner_module.planner_node(state)
    metadata = result["facts"]["metadata"]

    assert llm_client.calls == 1
    assert metadata["planner_mode"] == "plan_ready"
    assert "pending_clarify_request" not in metadata
    assert result["facts"]["plan"] == ["Step 1: Scan target", "Step 2: Review findings"]


@pytest.mark.asyncio
async def test_planner_uses_structured_contract_payload_when_content_not_json(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    client = _FakeStructuredLLMClient(
        {
            "mode": "plan_ready",
            "plan": ["Step 1: Discover hosts", "Step 2: Scan selected host"],
            "todo_list": ["Discover hosts", "Scan selected host"],
            "first_goal": "Discover hosts",
        }
    )
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: client)

    state = _build_state("full_access")
    state.facts.message = "Discover hosts then scan one host"

    result = await planner_module.planner_node(state)

    assert client.calls == 1
    assert result["facts"]["plan"] == ["Step 1: Discover hosts", "Step 2: Scan selected host"]
    assert result["facts"]["current_goal"] == "Discover hosts"


@pytest.mark.asyncio
async def test_planner_retries_when_scope_validation_fails(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    client = _FakeSequentialLLMClient(
        [
            """
            {
              "mode": "plan_ready",
              "plan": [
                "Step 1: Scan selected target for PostgreSQL on port 5432",
                "Step 2: Summarize PostgreSQL findings"
              ],
              "todo_list": [
                "Scan selected target for PostgreSQL on port 5432",
                "Summarize PostgreSQL findings"
              ],
              "first_goal": "Scan selected target for PostgreSQL on port 5432"
            }
            """,
            """
            {
              "mode": "plan_ready",
              "plan": [
                "Step 1: Discover online hosts in the reachable network segment",
                "Step 2: Select one discovered host and scan port 5432 for PostgreSQL"
              ],
              "todo_list": [
                "Discover online hosts in the reachable network segment",
                "Scan selected host for port 5432"
              ],
              "first_goal": "Discover online hosts in the reachable network segment"
            }
            """,
        ]
    )
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: client)

    state = _build_state("full_access")
    state.facts.message = "Scan network to find online hosts then scan one host for postgre port"

    result = await planner_module.planner_node(state)
    metadata = result["facts"]["metadata"]

    assert client.calls == 2
    assert metadata["planner_mode"] == "plan_ready"
    assert metadata["plan_validation"]["valid"] is True
    assert metadata["plan_validation"]["recovered_from"]["valid"] is False
    assert result["facts"]["plan"][0].lower().startswith("step 1: discover online hosts")


@pytest.mark.asyncio
async def test_planner_preserves_initial_plan_when_scope_validation_retry_fails(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    client = _FakeSequentialLLMClient(
        [
            """
            {
              "mode": "plan_ready",
              "plan": [
                "Step 1: Scan forbidden external host",
                "Step 2: Summarize forbidden external host"
              ],
              "todo_list": [
                "Scan forbidden external host",
                "Summarize forbidden external host"
              ],
              "first_goal": "Scan forbidden external host"
            }
            """,
            """
            {
              "mode": "plan_ready",
              "plan": [
                "Step 1: Still scan forbidden external host",
                "Step 2: Still summarize forbidden external host"
              ],
              "todo_list": [
                "Still scan forbidden external host",
                "Still summarize forbidden external host"
              ],
              "first_goal": "Still scan forbidden external host"
            }
            """,
        ]
    )
    validation_results = [
        {"valid": False, "violations": ["outside declared scope"]},
        {"valid": False, "violations": ["still outside declared scope"]},
    ]

    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(
        planner_generation_module,
        "validate_plan_against_scope",
        lambda *_args, **_kwargs: validation_results.pop(0),
    )

    state = _build_state("full_access")
    result = await planner_module.planner_node(state)
    metadata = result["facts"]["metadata"]

    assert client.calls == 2
    assert metadata["planner_mode"] == "plan_ready"
    assert metadata["plan_validation"] == {
        "valid": False,
        "violations": ["outside declared scope"],
    }
    assert metadata["plan_validation_retry"] == {
        "valid": False,
        "violations": ["still outside declared scope"],
    }
    assert result["facts"]["plan"] == [
        "Step 1: Scan forbidden external host",
        "Step 2: Summarize forbidden external host",
    ]


@pytest.mark.asyncio
async def test_planner_falls_back_when_llm_call_raises(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    llm_client = _FailingLLMClient(RuntimeError("planner call failed"))
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: llm_client)
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (
            ["Fallback step"],
            ["Fallback todo"],
            "Fallback goal",
        ),
    )

    state = _build_state("full_access")
    result = await planner_module.planner_node(state)

    assert llm_client.calls == 1
    assert result["facts"]["plan"] == ["Fallback step"]
    assert _todo_texts(result["facts"]["todo_list"]) == ["Fallback step"]
    assert result["facts"]["current_goal"] == "Fallback goal"


@pytest.mark.asyncio
async def test_planner_fails_after_invalid_clarify_contract_retry(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    client = _FakeSequentialLLMClient(
        [
            """
            {
              "mode": "clarify_required",
              "clarify_request": {
                "required_blockers": [
                  {
                    "slot": "target",
                    "question": "Which host?",
                    "input_type": "text",
                    "options": []
                  }
                ]
              }
            }
            """,
            """
            {
              "mode": "clarify_required",
              "clarify_request": {
                "required_blockers": [
                  {
                    "slot": "target",
                    "question": "Which host?",
                    "input_type": "select",
                    "options": []
                  }
                ]
              }
            }
            """,
        ]
    )
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: client)

    state = _build_state("full_access")
    result = await planner_module.planner_node(state)
    metadata = result["facts"]["metadata"]

    assert client.calls == 2
    assert metadata["planner_mode"] == "plan_failed"
    assert metadata["clarify_phase_status"] == "failed"
    assert "invalid clarification contract" in metadata["clarify_failure_message"]
    assert result["facts"]["plan"] == []


@pytest.mark.asyncio
async def test_planner_blocks_same_slot_clarify_loop_after_resolution(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    llm_client = _FakeLLMClient(
        """
        {
          "mode": "clarify_required",
          "plan": [],
          "todo_list": [],
          "first_goal": "",
          "clarify_request": {
            "required_blockers": [
              {
                "slot": "target",
                "question": "Which host should be scanned?",
                "input_type": "select",
                "options": ["10.0.0.1", "10.0.0.2"]
              }
            ]
          }
        }
        """
    )
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: llm_client)

    state = _build_state("full_access")
    state.facts.metadata.update(
        {
            "clarify_phase_status": "resolved",
            "clarify_slots_signature": "target",
            "clarified_context": {"target": "10.0.0.1"},
            "clarify_cycle_count": 1,
        }
    )

    result = await planner_module.planner_node(state)
    metadata = result["facts"]["metadata"]

    assert metadata["planner_mode"] == "plan_failed"
    assert metadata["clarify_phase_status"] == "failed"
    assert "loop detected" in metadata["clarify_failure_message"]
    assert result["facts"]["plan"] == []


@pytest.mark.asyncio
async def test_planner_does_not_increment_cycle_while_clarify_pending(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module

    def _raise_if_called(*_args, **_kwargs):
        raise AssertionError("LLM should not be called while clarify answers are missing")

    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", _raise_if_called)

    state = _build_state("full_access")
    state.facts.metadata.update(
        {
            "clarify_cycle_count": 3,
            "pending_clarify_request": {
                "required_blockers": [
                    {
                        "slot": "target",
                        "question": "Which host should be scanned?",
                        "input_type": "select",
                        "options": ["10.0.0.1", "10.0.0.2"],
                    }
                ]
            },
            "clarified_context": {},
        }
    )

    result = await planner_module.planner_node(state)
    metadata = result["facts"]["metadata"]
    assert metadata["planner_mode"] == "clarify_required"
    assert metadata["clarify_phase_status"] == "pending"
    assert metadata["clarify_cycle_count"] == 3


@pytest.mark.asyncio
async def test_planner_node_plan_approval_mode(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")))
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

    state = _build_state("plan")
    planned_state = await planner_module.planner_node(state)
    result = await plan_review_module.plan_review_node(planned_state)

    metadata = result["facts"]["metadata"]
    assert metadata["plan_approved"] is True
    assert metadata["plan_approval_action"] == "approve"
    assert len(metadata.get("todo_id_map", [])) == 2


@pytest.mark.asyncio
async def test_plan_review_interrupt_payload_keeps_todos_pending_before_approval(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
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
    captured_payload: dict = {}

    def _capture_interrupt(**kwargs):
        payload = kwargs.get("payload")
        if isinstance(payload, dict):
            captured_payload.update(payload)
        return {"action": "approve"}

    monkeypatch.setattr(
        plan_review_module,
        "request_plan_approval", _capture_interrupt)

    state = _build_state("plan")
    planned_state = await planner_module.planner_node(state)
    await plan_review_module.plan_review_node(planned_state)

    assert captured_payload["type"] == "plan_review"
    assert captured_payload["todo_list"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_plan_mode_true_plan_review_payload_preserves_contract_shape(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
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
        lambda *_args, **_kwargs: (
            ["Step 1: Collect evidence", "Step 2: Summarize findings"],
            ["Collect evidence", "Summarize findings"],
            "Collect evidence",
        ),
    )
    captured_payload: dict = {}

    def _capture_interrupt(**kwargs):
        payload = kwargs.get("payload")
        if isinstance(payload, dict):
            captured_payload.update(payload)
        return {"action": "approve"}

    monkeypatch.setattr(
        plan_review_module,
        "request_plan_approval", _capture_interrupt)

    state = _build_state("plan")
    state.facts.metadata.update(
        {
            "turn_sequence": 42,
            "turn_id": "task-1-turn-42",
            "reserved_message_id": 1001,
        }
    )

    planned_state = await planner_module.planner_node(state)
    await plan_review_module.plan_review_node(planned_state)

    assert set(captured_payload) == {
        "type",
        "interrupt_id",
        "goal",
        "plan_steps",
        "todo_list",
        "reasoning",
        "targets",
        "run_id",
        "plan_version",
        "turn_sequence",
        "turn_id",
        "reserved_message_id",
    }
    assert captured_payload["type"] == "plan_review"
    assert captured_payload["goal"] == "Collect evidence"
    assert captured_payload["plan_steps"] == [
        "Step 1: Collect evidence",
        "Step 2: Summarize findings",
    ]
    assert captured_payload["targets"] == []
    assert captured_payload["run_id"] == 42
    assert captured_payload["plan_version"] == 1
    assert captured_payload["turn_sequence"] == 42
    assert captured_payload["turn_id"] == "task-1-turn-42"
    assert captured_payload["reserved_message_id"] == 1001
    assert isinstance(captured_payload["interrupt_id"], str)
    assert [
        set(item)
        for item in captured_payload["todo_list"]
    ] == [{"id", "text", "status"}, {"id", "text", "status"}]
    assert [item["text"] for item in captured_payload["todo_list"]] == [
        "Step 1: Collect evidence",
        "Step 2: Summarize findings",
    ]
    assert [item["status"] for item in captured_payload["todo_list"]] == [
        "pending",
        "pending",
    ]


@pytest.mark.asyncio
async def test_planner_node_reject_action(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")))
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (["Step 1", "Step 2"], ["Todo 1"], "Goal"),
    )
    monkeypatch.setattr(
        plan_review_module,
        "request_plan_approval",
        lambda **_kwargs: {"action": "reject"},
    )

    state = _build_state("plan")
    planned_state = await planner_module.planner_node(state)
    result = await plan_review_module.plan_review_node(planned_state)

    metadata = result["facts"]["metadata"]
    assert metadata["plan_rejected"] is True
    assert "finalize" in result["facts"]["decision_history"][-1]


@pytest.mark.asyncio
async def test_planner_node_edit_action(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")))
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (["Step 1", "Step 2"], ["Todo 1"], "Goal"),
    )
    monkeypatch.setattr(
        plan_review_module,
        "request_plan_approval",
        lambda **_kwargs: {
            "action": "edit",
            "edited_goal": "New goal",
            "edited_plan_steps": ["New step 1", "New step 2"],
            "edited_todo_list": ["Todo 2"],
        },
    )

    state = _build_state("plan")
    planned_state = await planner_module.planner_node(state)
    result = await plan_review_module.plan_review_node(planned_state)

    assert result["facts"]["current_goal"] == "New goal"
    assert result["facts"]["plan"] == ["New step 1", "New step 2"]
    assert _todo_texts(result["facts"]["todo_list"]) == ["New step 1", "New step 2"]
    assert len(result["facts"]["metadata"].get("todo_id_map", [])) == 2


@pytest.mark.asyncio
async def test_plan_review_approve_emits_authoritative_event_with_run_and_version(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
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

    state = _build_state("plan")
    planned_state = await planner_module.planner_node(state)
    await plan_review_module.plan_review_node(planned_state, writer=writer)

    assert events
    assert events[0]["type"] in {"todo_progress", "plan_created"}
    assert isinstance(events[0].get("run_id"), int)
    assert isinstance(events[0].get("plan_version"), int)


@pytest.mark.asyncio
async def test_plan_review_prefers_todo_progress_delta_when_available(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
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
    monkeypatch.setattr(
        plan_review_module,
        "build_todo_stream_updates",
        lambda *_args, **_kwargs: [
            {"id": "todo-1", "text": "Todo 1", "status": "in_progress", "index": 0}
        ],
    )

    events = []

    def writer(event):
        events.append(event)

    state = _build_state("plan")
    planned_state = await planner_module.planner_node(state)
    await plan_review_module.plan_review_node(planned_state, writer=writer)

    assert events
    assert events[0]["type"] == "todo_progress"
    assert events[0]["todo_updates"][0]["status"] == "in_progress"
    assert isinstance(events[0].get("run_id"), int)
    assert isinstance(events[0].get("plan_version"), int)


@pytest.mark.asyncio
async def test_planner_node_emits_plan_created(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setenv("ENABLE_HITL_INTERRUPTS", "true")
    monkeypatch.setattr(hitl_helpers_module, "should_require_plan_approval", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")))
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (["Step 1", "Step 2"], ["Todo 1"], "Goal"),
    )

    events = []

    def writer(event):
        events.append(event)

    state = _build_state("full_access")
    planned_state = await planner_module.planner_node(state)
    await plan_review_module.plan_review_node(planned_state, writer=writer)

    assert events
    assert events[0]["type"] == "plan_created"
    assert events[0]["todo_list"][0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_planner_node_initializes_first_todo_in_progress(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setattr(hitl_helpers_module, "should_require_plan_approval", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(planner_generation_module, "resolve_llm_client", lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")))
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (["Step 1", "Step 2"], ["Todo 1"], "Goal"),
    )

    state = _build_state("full_access")
    planned_state = await planner_module.planner_node(state)

    todo_list = planned_state["facts"]["todo_list"]
    assert todo_list[0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_planner_initializes_runtime_budgets_and_plan_context(monkeypatch) -> None:
    from agent.graph.nodes import planner as planner_module
    from agent.providers.llm.core.exceptions import LLMConfigurationError

    monkeypatch.setattr(hitl_helpers_module, "should_require_plan_approval", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        planner_generation_module,
        "resolve_llm_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMConfigurationError("no key")),
    )
    monkeypatch.setattr(
        planner_response_module,
        "create_fallback_plan",
        lambda *_args, **_kwargs: (
            ["Step 1: Collect evidence", "Step 2: Summarize findings"],
            ["Collect evidence", "Summarize findings"],
            "Collect evidence",
        ),
    )

    state = _build_state("full_access")
    state.facts.capability = "deep_reasoning"
    state.facts.iterations = 3
    state.trace.observations = ["open port"]
    state.trace.executed_tools = [{"tool_id": "nmap"}]
    state.facts.metadata["tool_history"] = [{"tool": "nmap"}]

    planned_state = await planner_module.planner_node(state)
    metadata = planned_state["facts"]["metadata"]

    assert metadata["runtime_budgets"] == {
        "time_budget_ms": 300000,
        "remaining_iterations": 15,
        "remaining_tool_calls": 10,
    }
    assert metadata["plan_context"]["capability"] == "deep_reasoning"
    assert metadata["plan_context"]["goal"] == "Collect evidence"
    assert metadata["plan_context"]["findings_count"] == 3
    assert metadata["plan_context"]["iteration"] == 3
    assert isinstance(metadata["plan_context"]["created_at"], float)


# ---------------------------------------------------------------------------
# Phase 3 Task 3.3 guardrails: DR planner is a brief consumer only.
# ---------------------------------------------------------------------------


def test_planner_module_does_not_import_transcript_serialization() -> None:
    """Planner module MUST NOT re-import transcript plumbing.

    Task 3.3 removed every bundle-transcript read from
    ``agent.graph.nodes.planner``. This guard fails fast if a future
    change resurrects ``SECTION_RECENT_TRANSCRIPT``,
    ``METADATA_CONTEXT_BUNDLE_KEY``, ``project_for_planner``, or
    ``serialize_projection_to_section_map`` at the planner seam.
    """
    from agent.graph.nodes import planner as planner_module

    assert getattr(planner_module, "SECTION_RECENT_TRANSCRIPT", None) is None
    assert getattr(planner_module, "METADATA_CONTEXT_BUNDLE_KEY", None) is None
    assert getattr(planner_module, "project_for_planner", None) is None
    assert (
        getattr(planner_module, "serialize_projection_to_section_map", None)
        is None
    )


def test_build_planning_prompt_consumes_brief_without_transcript() -> None:
    """When the brief is populated and a bundle with distinctive transcript
    text is also in metadata, ``build_planning_prompt`` must render the
    brief fields and MUST NOT leak any transcript marker. This locks the
    Phase 3 Task 3.3 cutover at the wired planner helper, not just at
    the builder seam.
    """
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )
    from agent.graph.nodes.planner_prompting import build_planning_prompt

    brief = {
        "resolved_user_intent": "BRIEF_INTENT_TOKEN scan 10.0.0.7",
        "overall_goal": "BRIEF_GOAL_TOKEN enumerate services",
        "continuation_mode": "new_request",
        "next_operational_goal": "BRIEF_NEXT_GOAL_TOKEN run tcp scan",
        "success_condition": "BRIEF_SUCCESS_TOKEN list of open ports",
        "execution_readiness": "ready",
        "blocking_reason": None,
        "explicit_constraints": ["BRIEF_CONSTRAINT_TOKEN no udp"],
        "relevant_memory_fragments": [],
        "retrieval_hints": [],
        "request_contract": {
            "question_type": "multi_step",
            "answer_style": "normal",
            "terminal_when": "all_steps_done",
        },
        "target": {
            "resolved_target": "10.0.0.7",
            "target_status": "resolved",
            "target_source": "explicit_current_message",
            "prior_target_reuse": "allow",
        },
    }

    bundle = build_conversation_context_bundle(
        conversation_id="conv-guardrail",
        turn_id="turn-guardrail",
        turn_sequence=3,
        messages=[
            {
                "role": "user",
                "content": "TRANSCRIPT_USER_MARKER: scan 10.0.0.7",
            },
            {
                "role": "assistant",
                "content": "TRANSCRIPT_ASSISTANT_MARKER: working on it",
            },
        ],
        current_message="TRANSCRIPT_CURRENT_MARKER please continue",
    )

    metadata = {
        "working_memory": {"intent_brief": brief},
        METADATA_CONTEXT_BUNDLE_KEY: bundle,
    }

    prompt = build_planning_prompt(
        targets=["10.0.0.7"],
        metadata=metadata,
    )

    # Brief fields reach the prompt.
    assert "BRIEF_INTENT_TOKEN scan 10.0.0.7" in prompt
    assert "BRIEF_GOAL_TOKEN enumerate services" in prompt
    assert "BRIEF_NEXT_GOAL_TOKEN run tcp scan" in prompt
    assert "BRIEF_SUCCESS_TOKEN list of open ports" in prompt
    assert "BRIEF_CONSTRAINT_TOKEN no udp" in prompt

    # Transcript content must not leak, even though the bundle is present.
    for transcript_marker in (
        "TRANSCRIPT_USER_MARKER",
        "TRANSCRIPT_ASSISTANT_MARKER",
        "TRANSCRIPT_CURRENT_MARKER",
    ):
        assert transcript_marker not in prompt, (
            f"transcript content {transcript_marker!r} leaked into DR "
            "planner prompt despite Task 3.3 cutover"
        )

    for structural_marker in (
        "<turn",
        "</turn>",
        "role=user",
        "role=assistant",
        "latest=true",
        "Conversation (oldest -> newest",
        "Recent conversation",
    ):
        assert structural_marker not in prompt, (
            f"transcript structural marker {structural_marker!r} leaked "
            "into DR planner prompt"
        )
