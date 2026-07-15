"""Run production LangGraph prompt flows with mocked tool outcomes.

This runner executes the same LangGraph handlers used in production and keeps
the real prompt construction + LLM call order intact. It replaces only tool
execution results, allowing prompt tuning without running real tools.

Outputs:
- Full ordered LLM request/response capture per scenario
- Final assistant output and key graph metadata
- Mock tool call trace showing injected outputs used by the graph
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence
from unittest.mock import patch

# Keep backend imports safe in standalone runs.
os.environ.setdefault("DATABASE_URL", "sqlite:///./drowai_prompt_mock_suite.db")
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agent.providers.llm.core.base import LLMStreamingResponse  # noqa: E402
from agent.providers.llm.factory import LLMClientFactory  # noqa: E402
from agent.tool_runtime.coordinator import (  # noqa: E402
    ToolExecutionOutcome,
)
from backend.services.langgraph_chat.contracts import (  # noqa: E402
    AgentMode,
    ChatInputs,
    ExecutionMode,
)
from backend.services.langgraph_chat.facade import LangGraphChatFacade  # noqa: E402
from backend.services.langgraph_chat.routing.selectors import ChatBranch  # noqa: E402

try:
    from langgraph.checkpoint.memory import MemorySaver
except Exception as exc:  # pragma: no cover - dependency guard
    raise RuntimeError("langgraph MemorySaver is required for this runner.") from exc

DEFAULT_OUTPUT_DIR = ROOT_DIR / "docs" / "testing" / "mock_prompt_runs"


@dataclass(frozen=True)
class MockToolResult:
    """Single mocked tool outcome consumed in sequence per scenario."""

    summary: str
    result: Mapping[str, Any]
    tool_id: Optional[str] = None
    parameters: Optional[Mapping[str, Any]] = None
    duration: float = 0.2


@dataclass(frozen=True)
class ScenarioSpec:
    """Config for one prompt-replay scenario."""

    scenario_id: str
    execution_mode: ExecutionMode
    message: str
    history: Sequence[Mapping[str, Any]]
    mock_tool_results: Sequence[MockToolResult]


class _CheckpointerWithSetup:
    """Memory saver wrapper with setup() for handler compatibility."""

    def __init__(self, checkpointer: MemorySaver) -> None:
        self._inner = checkpointer

    async def setup(self) -> None:
        return None

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


class DummyCheckpointerService:
    """Provide one shared in-memory checkpointer across a run."""

    def __init__(self) -> None:
        self._checkpointer = _CheckpointerWithSetup(MemorySaver())

    @asynccontextmanager
    async def get_checkpointer(self, task_id: int):  # noqa: ARG002
        yield self._checkpointer


def _to_jsonable(value: Any) -> Any:
    """Convert nested values into JSON-safe primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "__dict__"):
        return _to_jsonable(vars(value))
    return str(value)


def _usage_to_dict(usage: Any) -> Optional[Dict[str, Any]]:
    """Normalize usage object to dict."""

    if usage is None:
        return None
    if isinstance(usage, dict):
        return _to_jsonable(usage)
    return _to_jsonable(
        {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "model": getattr(usage, "model", None),
            "provider": getattr(usage, "provider", None),
            "cached_tokens": getattr(usage, "cached_tokens", None),
            "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
        }
    )


def _relative_origin_from_stack() -> str:
    """Find first non-provider caller to annotate each LLM request."""

    skip_markers = (
        "/agent/providers/llm/",
        "/scripts/run_langgraph_prompt_mock_suite.py",
    )
    for frame in inspect.stack()[2:]:
        filename = frame.filename.replace("\\", "/")
        if any(marker in filename for marker in skip_markers):
            continue
        try:
            relative = Path(filename).resolve().relative_to(ROOT_DIR.resolve())
        except Exception:
            relative = Path(filename).name
        return f"{relative}:{frame.function}"
    return "unknown"


class PromptRecorder:
    """Collect ordered LLM request/response payloads."""

    def __init__(self) -> None:
        self._calls: List[Dict[str, Any]] = []

    def start_call(self, *, method: str, model: str, request: Dict[str, Any]) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "sequence": len(self._calls) + 1,
            "method": method,
            "model": model,
            "origin": _relative_origin_from_stack(),
            "request": _to_jsonable(request),
            "response": None,
        }
        self._calls.append(entry)
        return entry

    @property
    def calls(self) -> List[Dict[str, Any]]:
        return self._calls


class RecordingLLMClient:
    """Proxy LLMClient that records every request in execution order."""

    def __init__(self, delegate: Any, recorder: PromptRecorder) -> None:
        self._delegate = delegate
        self._recorder = recorder

    @property
    def model(self) -> str:
        return str(getattr(self._delegate, "model", "unknown"))

    async def chat_with_usage(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> Any:
        call = self._recorder.start_call(
            method="chat_with_usage",
            model=self.model,
            request={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            },
        )
        response = await self._delegate.chat_with_usage(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            **kwargs,
        )
        call["response"] = {
            "content": getattr(response, "content", ""),
            "usage": _usage_to_dict(getattr(response, "usage", None)),
        }
        return response

    async def chat_with_tools_with_usage(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> Any:
        call = self._recorder.start_call(
            method="chat_with_tools_with_usage",
            model=self.model,
            request={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": tools,
                "tool_choice": tool_choice,
                "kwargs": kwargs,
            },
        )
        response = await self._delegate.chat_with_tools_with_usage(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )
        tool_calls = []
        for tool_call in getattr(response, "tool_calls", []) or []:
            tool_calls.append(
                {
                    "id": getattr(tool_call, "id", None),
                    "name": getattr(tool_call, "name", None),
                    "arguments": getattr(tool_call, "arguments", None),
                }
            )
        call["response"] = {
            "content": getattr(response, "content", ""),
            "tool_calls": tool_calls,
            "usage": _usage_to_dict(getattr(response, "usage", None)),
        }
        return response

    async def stream_chat_messages_with_usage(
        self,
        messages: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> LLMStreamingResponse:
        call = self._recorder.start_call(
            method="stream_chat_messages_with_usage",
            model=self.model,
            request={
                "messages": messages,
                "kwargs": kwargs,
            },
        )
        response = await self._delegate.stream_chat_messages_with_usage(messages, **kwargs)
        chunks: List[str] = []
        cached_usage: Dict[str, Any] = {"usage": None}

        async def _iter() -> Any:
            async for chunk in response.content_iterator:
                chunks.append(chunk)
                yield chunk
            call["response"] = {
                "content": "".join(chunks),
                "usage": cached_usage["usage"],
            }

        def _get_usage() -> Any:
            usage = response.get_final_usage()
            cached_usage["usage"] = _usage_to_dict(usage)
            return usage

        return LLMStreamingResponse(
            content_iterator=_iter(),
            get_final_usage=_get_usage,
        )

    async def chat(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> Any:
        call = self._recorder.start_call(
            method="chat",
            model=self.model,
            request={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "kwargs": kwargs,
            },
        )
        response = await self._delegate.chat(system_prompt, user_prompt, **kwargs)
        call["response"] = {"content": response}
        return response

    async def chat_messages(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        call = self._recorder.start_call(
            method="chat_messages",
            model=self.model,
            request={"messages": messages, "kwargs": kwargs},
        )
        response = await self._delegate.chat_messages(messages, **kwargs)
        call["response"] = {"content": response}
        return response

    async def stream_chat_messages(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        call = self._recorder.start_call(
            method="stream_chat_messages",
            model=self.model,
            request={"messages": messages, "kwargs": kwargs},
        )
        stream = self._delegate.stream_chat_messages(messages, **kwargs)
        chunks: List[str] = []

        async def _iter() -> Any:
            async for chunk in stream:
                chunks.append(chunk)
                yield chunk
            call["response"] = {"content": "".join(chunks)}

        return _iter()

    async def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_choice: Any = "auto",
        **kwargs: Any,
    ) -> Any:
        call = self._recorder.start_call(
            method="chat_with_tools",
            model=self.model,
            request={
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "tools": tools,
                "tool_choice": tool_choice,
                "kwargs": kwargs,
            },
        )
        response = await self._delegate.chat_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )
        call["response"] = _to_jsonable(response)
        return response

    def __getattr__(self, item: str) -> Any:
        return getattr(self._delegate, item)


class MockToolRunner:
    """Sequential mock injector for ToolExecutionCoordinator.run()."""

    def __init__(self, templates: Sequence[MockToolResult]) -> None:
        if not templates:
            raise ValueError("At least one mock tool result is required.")
        self._templates = list(templates)
        self._index = 0
        self.call_log: List[Dict[str, Any]] = []

    async def run(self, request: Any) -> ToolExecutionOutcome:
        template_idx = self._index if self._index < len(self._templates) else len(self._templates) - 1
        self._index += 1
        template = self._templates[template_idx]

        metadata = getattr(request, "metadata", {}) or {}
        planner_plan = metadata.get("planner_plan") if isinstance(metadata, dict) else None
        selected_tools = []
        if isinstance(planner_plan, dict):
            selected_tools = list(planner_plan.get("selected_tools") or [])

        chosen_tool_id = selected_tools[0] if selected_tools else template.tool_id or "unknown_tool"
        planner_params = {}
        if isinstance(planner_plan, dict):
            planner_params = (
                planner_plan.get("tool_parameters", {}).get(chosen_tool_id, {}) or {}
            )
        parameters = dict(planner_params or template.parameters or {})
        result = deepcopy(dict(template.result))
        result.setdefault("status", "success")
        result.setdefault("success", result.get("status") == "success")
        result.setdefault("observation", template.summary)
        result.setdefault("stdout", "")
        result.setdefault("stderr", "")
        result.setdefault("stdout_excerpt", str(result.get("stdout", ""))[:500])
        result.setdefault("stderr_excerpt", str(result.get("stderr", ""))[:500])
        result.setdefault("duration", template.duration)
        result.setdefault("exit_code", 0 if result.get("success") else 1)

        self.call_log.append(
            {
                "sequence": len(self.call_log) + 1,
                "capability": getattr(request, "capability", None),
                "targets": list(getattr(request, "targets", []) or []),
                "tool_id": chosen_tool_id,
                "parameters": parameters,
                "result_status": result.get("status"),
            }
        )

        return ToolExecutionOutcome(
            tool_id=chosen_tool_id,
            parameters=parameters,
            catalog=[],
            result=result,
            summary=template.summary,
            reasoning=[template.summary],
            duration=template.duration,
        )


def _scenario_specs() -> Dict[str, ScenarioSpec]:
    """Build fixed simple-tool and deep-reasoning replay scenarios."""

    simple_tool_prompt = "scan 127.0.0.1 for postgre port."
    deep_reasoning_prompt = (
        "Scan network to find online hosts then scan 1 host for postgre port. Short answer"
    )

    simple_tool_result = MockToolResult(
        summary="Port 5432/tcp is open on 127.0.0.1 and service looks like PostgreSQL.",
        result={
            "status": "success",
            "success": True,
            "stdout": (
                "Nmap scan report for 127.0.0.1\n"
                "PORT     STATE SERVICE\n"
                "5432/tcp open  postgresql\n"
                "Service Info: PostgreSQL"
            ),
            "stderr": "",
            "observation": "5432/tcp open postgresql on 127.0.0.1",
            "exit_code": 0,
        },
    )

    dr_result_1 = MockToolResult(
        summary="Host discovery found two live hosts; 172.17.0.3 selected for follow-up.",
        result={
            "status": "success",
            "success": True,
            "stdout": (
                "Nmap scan report for 172.17.0.2\nHost is up\n\n"
                "Nmap scan report for 172.17.0.3\nHost is up"
            ),
            "stderr": "",
            "observation": "Live hosts: 172.17.0.2, 172.17.0.3",
            "exit_code": 0,
        },
    )
    dr_result_2 = MockToolResult(
        summary="Targeted port scan shows PostgreSQL open on 172.17.0.3:5432.",
        result={
            "status": "success",
            "success": True,
            "stdout": (
                "Nmap scan report for 172.17.0.3\n"
                "PORT     STATE SERVICE\n"
                "5432/tcp open  postgresql"
            ),
            "stderr": "",
            "observation": "172.17.0.3 has 5432/tcp open (postgresql)",
            "exit_code": 0,
        },
    )

    return {
        "simple_tool": ScenarioSpec(
            scenario_id="simple_tool",
            execution_mode=ExecutionMode.SIMPLE_TOOL,
            message=simple_tool_prompt,
            history=(),
            mock_tool_results=(simple_tool_result,),
        ),
        "deep_reasoning": ScenarioSpec(
            scenario_id="deep_reasoning",
            execution_mode=ExecutionMode.DEEP_REASONING,
            message=deep_reasoning_prompt,
            history=(),
            mock_tool_results=(dr_result_1, dr_result_2),
        ),
    }


async def _run_scenario(
    *,
    scenario: ScenarioSpec,
    api_key: str,
    model: str,
    task_id: int,
) -> Dict[str, Any]:
    """Execute one scenario while recording prompts and injecting tool outputs."""

    recorder = PromptRecorder()
    tool_runner = MockToolRunner(scenario.mock_tool_results)

    async def _inline_completion_callback(*, llm_func, result_holder, **kwargs):  # noqa: ANN001
        await llm_func(lambda _event: None, result_holder)
        if False:  # pragma: no cover - async generator contract
            yield {}

    original_get_client = LLMClientFactory.get_client

    def _recording_get_client(cls, *, model: str, api_key: str, **kwargs: Any) -> Any:
        delegate = original_get_client(model=model, api_key=api_key, **kwargs)
        return RecordingLLMClient(delegate=delegate, recorder=recorder)

    async def _mock_coordinator_run(_self, request):  # noqa: ANN001
        return await tool_runner.run(request)

    checkpointer_service = DummyCheckpointerService()
    with patch.object(LLMClientFactory, "get_client", classmethod(_recording_get_client)), patch(
        "agent.tool_runtime.coordinator.ToolExecutionCoordinator.run",
        _mock_coordinator_run,
    ), patch(
        "backend.services.langgraph_chat.handlers.simple_tool_handler.run_turn_with_completion_callback",
        _inline_completion_callback,
    ), patch(
        "backend.services.langgraph_chat.handlers.deep_reasoning_handler.run_turn_with_completion_callback",
        _inline_completion_callback,
    ), patch(
        "backend.services.langgraph_chat.facade.ENABLE_LANGGRAPH_SIMPLE_TOOL",
        True,
    ), patch(
        "backend.services.langgraph_chat.facade.ENABLE_LANGGRAPH_DEEP_REASONING",
        True,
    ):
        facade = LangGraphChatFacade(checkpointer_service=checkpointer_service)
        chat_inputs = ChatInputs(
            task_id=task_id,
            user_id=1,
            message=scenario.message,
            conversation_id=None,
            history=list(scenario.history),
            api_key=api_key,
            model=model,
            requested_mode=scenario.execution_mode,
            agent_mode=AgentMode.FULL_ACCESS,
        )
        metadata = {
            "turn_id": f"task-{task_id}-turn-1",
            "turn_number": 1,
            "turn_sequence": 1,
        }

        runtime_config = facade._context_builder.build_runtime_config(  # noqa: SLF001
            chat_inputs=chat_inputs,
            metadata=metadata,
        )
        await facade._intent_classifier.enrich_runtime_config(runtime_config)  # noqa: SLF001
        runtime_config.execution_mode = scenario.execution_mode

        branch_map = {
            ExecutionMode.NORMAL_CHAT: ChatBranch.NORMAL_CHAT,
            ExecutionMode.SIMPLE_TOOL: ChatBranch.SIMPLE_TOOL,
            ExecutionMode.DEEP_REASONING: ChatBranch.DEEP_REASONING,
        }
        branch = branch_map[scenario.execution_mode]
        result = await facade._handlers[branch].handle(runtime_config)  # noqa: SLF001

    interactive_state = result.interactive_state
    facts_metadata: Dict[str, Any] = {}
    decision_history: List[str] = []
    executed_tools: List[Dict[str, Any]] = []
    if interactive_state is not None:
        facts_metadata = dict(interactive_state.facts.metadata or {})
        decision_history = list(interactive_state.facts.decision_history or [])
        for record in interactive_state.trace.executed_tools or []:
            executed_tools.append(
                {
                    "tool_id": record.tool_id,
                    "status": record.status,
                    "observation": record.observation,
                    "stdout_excerpt": record.stdout_excerpt,
                    "stderr_excerpt": record.stderr_excerpt,
                }
            )

    return {
        "scenario_id": scenario.scenario_id,
        "mode": scenario.execution_mode.value,
        "message": scenario.message,
        "llm_calls": recorder.calls,
        "llm_call_count": len(recorder.calls),
        "tool_calls_mocked": tool_runner.call_log,
        "tool_call_count": len(tool_runner.call_log),
        "final_text": result.final_text,
        "result_metadata": _to_jsonable(result.metadata),
        "decision_history": decision_history,
        "executed_tools": executed_tools,
        "state_snapshot": {
            "synthesized_output": _to_jsonable(facts_metadata.get("synthesized_output")),
            "last_tool_result": _to_jsonable(facts_metadata.get("last_tool_result")),
            "tool_history_count": len(facts_metadata.get("tool_history") or []),
            "user_goal_achieved": facts_metadata.get("user_goal_achieved"),
        },
    }


def _write_report(
    *,
    output_dir: Path,
    run_id: str,
    scenario_result: Dict[str, Any],
) -> Path:
    """Persist one scenario report JSON file."""

    output_dir.mkdir(parents=True, exist_ok=True)
    scenario_id = str(scenario_result.get("scenario_id"))
    target_path = output_dir / f"{run_id}_{scenario_id}.json"
    target_path.write_text(
        json.dumps(scenario_result, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    latest_path = output_dir / f"latest_{scenario_id}.json"
    latest_path.write_text(
        json.dumps(scenario_result, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return target_path


async def _run(args: argparse.Namespace) -> int:
    """Execute selected scenarios and write prompt capture artifacts."""

    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError(
            "OpenAI API key is required. Pass --api-key or set OPENAI_API_KEY."
        )

    scenarios = _scenario_specs()
    selected_ids: List[str]
    if args.scenario == "all":
        selected_ids = ["simple_tool", "deep_reasoning"]
    else:
        selected_ids = [args.scenario]

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir).resolve()
    all_results: List[Dict[str, Any]] = []

    for offset, scenario_id in enumerate(selected_ids):
        scenario = scenarios[scenario_id]
        task_id = args.task_id_base + offset
        result = await _run_scenario(
            scenario=scenario,
            api_key=api_key,
            model=args.model,
            task_id=task_id,
        )
        report_path = _write_report(
            output_dir=output_dir,
            run_id=run_id,
            scenario_result=result,
        )
        all_results.append(
            {
                "scenario_id": scenario_id,
                "report_path": str(report_path),
                "final_text": result.get("final_text"),
                "llm_call_count": result.get("llm_call_count"),
                "tool_call_count": result.get("tool_call_count"),
            }
        )

    summary = {
        "run_id": run_id,
        "model": args.model,
        "scenarios": all_results,
    }
    (output_dir / f"{run_id}_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "latest_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(summary, indent=2, ensure_ascii=True))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run production LangGraph prompting with mocked tool outputs and capture "
            "exact ordered LLM requests."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=["simple_tool", "deep_reasoning", "all"],
        default="all",
        help="Scenario to run.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="OpenAI API key. If omitted, OPENAI_API_KEY is used.",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o-mini",
        help="Model identifier used for all LLM requests.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory where JSON run reports are written.",
    )
    parser.add_argument(
        "--task-id-base",
        type=int,
        default=93000,
        help="Base task id used to allocate synthetic task ids per scenario.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
