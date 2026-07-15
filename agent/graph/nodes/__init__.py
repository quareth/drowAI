"""Node scaffolding for LangGraph orchestration.

Active nodes used by deep_reasoning_builder.py and simple_tool_builder.py.

Archived nodes (preserved for future use) are in _archive/:
- check_container_node: Container health check before tool execution
- handle_container_error_node: Graceful error handling when container unavailable
"""

from .classification import classify_turn
from .decision_router import decision_router
from .finalize import finalize_results
from .finalizer import finalize_turn
from .handle_unavailable_tools import handle_unavailable_tools_node
from .observation_adapter import adapt_to_observations
from .plan_review import plan_review_node
from .planner import planner_node
from .post_tool_reasoning import post_tool_reasoning
from .reflect import reflect_node
from .select_tool_categories import select_tool_categories_node
from .simple_chat import run_simple_chat
from .simple_chat_post import post_process_simple_chat
from .synthesis import synthesis_node
from .think_more import think_more_node
from .tool_articulation import articulate_tool_intent
from .tool_synthesizer import synthesize_tool_output

__all__ = [
    # Classification & Routing
    "classify_turn",
    "decision_router",

    # Planning
    "planner_node",
    "plan_review_node",
    "handle_unavailable_tools_node",

    # Tool Execution Flow
    "select_tool_categories_node",
    "articulate_tool_intent",
    "synthesize_tool_output",

    # Deep Reasoning
    "adapt_to_observations",
    "post_tool_reasoning",
    "think_more_node",
    "reflect_node",
    "synthesis_node",

    # Simple Chat
    "run_simple_chat",
    "post_process_simple_chat",

    # Finalizers (unified for simple-tool and deep reasoning)
    "finalize_results",
    "finalize_turn",
]
