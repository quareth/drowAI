"""
Unit tests for select_tool_categories retry behavior.

Tests verify that:
1. Normal path unchanged: Non-retry requests use LLM category selection
2. Alternative tool retry: Reflection suggests different tool, state updated correctly
3. Same tool retry with params: Reflection suggests same tool with modified params
4. Invalid alternative tool: Falls back to same tool retry
5. Missing reflection suggestions: Treated as normal request
6. Reflection suggestions cleared: Metadata cleaned after use
7. Categories determined: Retry tool categories set in metadata
"""

import pytest
from unittest.mock import patch, AsyncMock
from typing import Dict, Any

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.nodes.select_tool_categories import select_tool_categories_node
from agent.graph.state import InteractiveState, FactsState, TraceState


def _install_bundle_on_state(state: Dict[str, Any]) -> None:
    """Install an empty ConversationContextBundle on ``state.facts.metadata``.

    Phase 5 cutover: the category selector raises ``RuntimeError`` when
    ``metadata[context_bundle]`` is missing. Direct-node tests that
    bypass the facade must populate the bundle themselves.
    """
    metadata = state["facts"].setdefault("metadata", {})
    if METADATA_CONTEXT_BUNDLE_KEY not in metadata:
        metadata[METADATA_CONTEXT_BUNDLE_KEY] = build_conversation_context_bundle(
            conversation_id="conv-retry-tests",
            turn_id="turn-retry-tests",
            turn_sequence=0,
            messages=[],
        )


@pytest.fixture
def base_state() -> Dict[str, Any]:
    """Base state without retry metadata."""
    return {
        "facts": {
            "task_id": 1,
            "message": "scan target 192.168.1.1",
            "selected_tool": None,
            "tool_parameters": {},
            "metadata": {},
        },
        "trace": {
            "history": [],
            "reasoning": [],
        }
    }


@pytest.fixture
def retry_state_alternative_tool() -> Dict[str, Any]:
    """State with reflection suggestions for alternative tool."""
    return {
        "facts": {
            "task_id": 1,
            "message": "scan target 192.168.1.1",
            "selected_tool": "information_gathering.network_discovery.ping",
            "tool_parameters": {
                "information_gathering.network_discovery.ping": {"target": "192.168.1.1"}
            },
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": "information_gathering.route_analysis.traceroute",
                    "same_tool_retry": False,
                    "suggested_params": {"target": "192.168.1.1"},
                    "reasoning": "Network unreachable, traceroute can reveal routing issues",
                    "can_recover": True,
                    "failure_category": "network_error"
                },
                "retry_tracking": {"count": 1}
            }
        },
        "trace": {
            "history": [],
            "reasoning": [],
        }
    }


@pytest.fixture
def retry_state_same_tool() -> Dict[str, Any]:
    """State with reflection suggestions for same tool with modified params."""
    return {
        "facts": {
            "task_id": 1,
            "message": "scan ports on 192.168.1.1",
            "selected_tool": "information_gathering.network_discovery.nmap",
            "tool_parameters": {
                "information_gathering.network_discovery.nmap": {
                    "target": "192.168.1.1",
                    "ports": "80"
                }
            },
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": None,
                    "same_tool_retry": True,
                    "suggested_params": {"ports": "1-65535"},
                    "reasoning": "Limited port scan found nothing, retry with full range",
                    "can_recover": True,
                    "failure_category": "empty_results"
                },
                "retry_tracking": {"count": 1}
            }
        },
        "trace": {
            "history": [],
            "reasoning": [],
        }
    }


@pytest.mark.asyncio
async def test_retry_with_alternative_tool(retry_state_alternative_tool):
    """Test retry path with alternative tool suggestion."""
    result = await select_tool_categories_node(retry_state_alternative_tool)

    # Verify retry guidance is advisory, not executable state mutation
    assert "facts" in result
    updated_facts = result["facts"]
    assert updated_facts["selected_tool"] == "information_gathering.network_discovery.ping"
    assert "information_gathering.route_analysis.traceroute" in updated_facts["metadata"]["next_tool_hint"]

    # Verify reflection suggestions cleared
    assert "reflection_suggestions" not in updated_facts["metadata"]

    # Verify reasoning logged
    assert "trace" in result
    updated_trace = result["trace"]
    assert any("Retry attempt" in str(r) for r in updated_trace.get("reasoning", []))

    # Verify categories determined
    assert "selected_categories" in updated_facts["metadata"]
    assert "information_gathering" in updated_facts["metadata"]["selected_categories"]


@pytest.mark.asyncio
async def test_retry_with_same_tool_modified_params(retry_state_same_tool):
    """Test retry path with same tool but modified params."""
    result = await select_tool_categories_node(retry_state_same_tool)

    # Verify same tool kept
    assert "facts" in result
    updated_facts = result["facts"]
    assert updated_facts["selected_tool"] == "information_gathering.network_discovery.nmap"

    # Verify params are not mutated directly; retry guidance is prompt context.
    tool_params = updated_facts["tool_parameters"]["information_gathering.network_discovery.nmap"]
    assert tool_params["target"] == "192.168.1.1"
    assert tool_params["ports"] == "80"
    assert "1-65535" in updated_facts["metadata"]["next_tool_hint"]

    # Verify reflection suggestions cleared
    assert "reflection_suggestions" not in updated_facts["metadata"]

    # Verify reasoning logged
    assert "trace" in result
    updated_trace = result["trace"]
    assert any("Retry attempt" in str(r) for r in updated_trace.get("reasoning", []))


@pytest.mark.asyncio
async def test_invalid_alternative_tool_fallback():
    """Test that invalid alternative tool falls back gracefully."""
    state = {
        "facts": {
            "task_id": 1,
            "message": "scan target",
            "selected_tool": "information_gathering.network_discovery.ping",
            "tool_parameters": {
                "information_gathering.network_discovery.ping": {"target": "192.168.1.1"}
            },
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": "nonexistent.tool.invalid",
                    "same_tool_retry": False,
                    "suggested_params": {},
                    "reasoning": "Try something else",
                    "can_recover": True,
                    "failure_category": "unknown"
                },
                "retry_tracking": {"count": 1}
            }
        },
        "trace": {
            "history": [],
            "reasoning": [],
        }
    }
    
    with patch("agent.tools.tool_registry.tool_exists", return_value=False):
        # Execute node
        result = await select_tool_categories_node(state)
        
        # Should fallback to current tool (same-tool retry)
        assert "facts" in result
        updated_facts = result["facts"]
        assert updated_facts["selected_tool"] == "information_gathering.network_discovery.ping"


@pytest.mark.asyncio
async def test_normal_path_unchanged(base_state):
    """Test that normal requests still use LLM category selection."""
    # Mock LLM call
    mock_llm_client = AsyncMock()
    mock_llm_client.chat = AsyncMock(return_value='{"selected_categories": ["information_gathering"], "reasoning": "Network scan"}')
    
    with patch("agent.graph.nodes.select_tool_categories.resolve_llm_client", return_value=mock_llm_client), \
         patch("agent.tools.category_utils.get_tool_categories", return_value=["information_gathering", "exploitation_tools"]), \
         patch(
             "agent.tools.category_utils.get_category_descriptions",
             return_value={
                 "information_gathering": "Network tools",
                 "exploitation_tools": "Exploitation tooling",
             },
         ):
        
        # Add API key to state
        base_state["facts"]["metadata"]["api_key"] = "test-key"
        _install_bundle_on_state(base_state)

        # Execute node
        result = await select_tool_categories_node(base_state)

        # Verify LLM was called
        assert mock_llm_client.chat.called

        # Verify categories selected
        assert "facts" in result
        updated_facts = result["facts"]
        assert "selected_categories" in updated_facts["metadata"]
        assert "information_gathering" in updated_facts["metadata"]["selected_categories"]


@pytest.mark.asyncio
async def test_missing_reflection_suggestions_treated_as_normal(base_state):
    """Test that state without reflection suggestions uses normal flow."""
    # Mock LLM call
    mock_llm_client = AsyncMock()
    mock_llm_client.chat = AsyncMock(return_value='{"selected_categories": ["information_gathering"], "reasoning": "Network scan"}')
    
    with patch("agent.graph.nodes.select_tool_categories.resolve_llm_client", return_value=mock_llm_client), \
         patch("agent.tools.category_utils.get_tool_categories", return_value=["information_gathering"]), \
         patch(
             "agent.tools.category_utils.get_category_descriptions",
             return_value={"information_gathering": "Network tools"},
         ):
        
        # Add API key to state
        base_state["facts"]["metadata"]["api_key"] = "test-key"
        _install_bundle_on_state(base_state)

        # Execute node
        result = await select_tool_categories_node(base_state)

        # Verify LLM was called (normal path)
        assert mock_llm_client.chat.called


@pytest.mark.asyncio
async def test_reflection_suggestions_cleared(retry_state_alternative_tool):
    """Test that reflection suggestions are cleared after use."""
    # Verify reflection suggestions exist before
    assert "reflection_suggestions" in retry_state_alternative_tool["facts"]["metadata"]

    # Execute node
    result = await select_tool_categories_node(retry_state_alternative_tool)

    # Verify reflection suggestions cleared after
    assert "facts" in result
    updated_facts = result["facts"]
    assert "reflection_suggestions" not in updated_facts["metadata"]


@pytest.mark.asyncio
async def test_categories_determined_for_retry_tool(retry_state_alternative_tool):
    """Test that categories are determined for retry tools."""
    # Execute node
    result = await select_tool_categories_node(retry_state_alternative_tool)

    # Verify categories set in metadata
    assert "facts" in result
    updated_facts = result["facts"]
    assert "selected_categories" in updated_facts["metadata"]

    # Should extract category from tool_id
    categories = updated_facts["metadata"]["selected_categories"]
    assert "information_gathering" in categories


@pytest.mark.asyncio
async def test_no_clear_suggestion_fallback():
    """Test fallback behavior when no clear retry suggestion provided."""
    state = {
        "facts": {
            "task_id": 1,
            "message": "scan target",
            "selected_tool": "information_gathering.network_discovery.ping",
            "tool_parameters": {
                "information_gathering.network_discovery.ping": {"target": "192.168.1.1"}
            },
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": None,
                    "same_tool_retry": False,  # No clear suggestion
                    "suggested_params": {},
                    "reasoning": "Unknown issue",
                    "can_recover": False,
                    "failure_category": "unknown"
                },
                "retry_tracking": {"count": 1}
            }
        },
        "trace": {
            "history": [],
            "reasoning": [],
        }
    }
    
    result = await select_tool_categories_node(state)

    # Should keep current tool as fallback
    assert "facts" in result
    updated_facts = result["facts"]
    assert updated_facts["selected_tool"] == "information_gathering.network_discovery.ping"


@pytest.mark.asyncio
async def test_missing_current_tool_fallback():
    """Test fallback when no current tool is selected."""
    state = {
        "facts": {
            "task_id": 1,
            "message": "scan target",
            "selected_tool": None,  # No current tool
            "tool_parameters": {},
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": None,
                    "same_tool_retry": True,
                    "suggested_params": {},
                    "reasoning": "Retry same tool",
                    "can_recover": True,
                    "failure_category": "unknown"
                },
                "retry_tracking": {"count": 1}
            }
        },
        "trace": {
            "history": [],
            "reasoning": [],
        }
    }
    
    result = await select_tool_categories_node(state)
        
    # Should not invent an executable fallback tool without a canonical batch.
    assert "facts" in result
    updated_facts = result["facts"]
    assert updated_facts["selected_tool"] is None
    assert "next_tool_hint" in updated_facts["metadata"]


@pytest.mark.asyncio
async def test_metrics_tracked_for_alternative_tool():
    """Test that metrics are tracked for alternative tool retries."""
    state = {
        "facts": {
            "task_id": 1,
            "message": "scan target",
            "selected_tool": "information_gathering.network_discovery.ping",
            "tool_parameters": {"information_gathering.network_discovery.ping": {}},
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": "information_gathering.route_analysis.traceroute",
                    "same_tool_retry": False,
                    "suggested_params": {},
                    "reasoning": "Try alternative",
                    "can_recover": True,
                    "failure_category": "network_error"
                },
                "retry_tracking": {"count": 1}
            }
        },
        "trace": {"history": [], "reasoning": []}
    }
    
    with patch("agent.graph.nodes.select_tool_categories.safe_inc") as mock_inc:
        # Execute node
        await select_tool_categories_node(state)
        
        # Verify metrics incremented
        mock_inc.assert_called_with("simple_tool_retry_alternative_tool")


@pytest.mark.asyncio
async def test_metrics_tracked_for_same_tool():
    """Test that metrics are tracked for same tool retries."""
    state = {
        "facts": {
            "task_id": 1,
            "message": "scan target",
            "selected_tool": "information_gathering.network_discovery.nmap",
            "tool_parameters": {"information_gathering.network_discovery.nmap": {}},
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": None,
                    "same_tool_retry": True,
                    "suggested_params": {},
                    "reasoning": "Retry same tool",
                    "can_recover": True,
                    "failure_category": "empty_results"
                },
                "retry_tracking": {"count": 1}
            }
        },
        "trace": {"history": [], "reasoning": []}
    }
    
    with patch("agent.graph.nodes.select_tool_categories.safe_inc") as mock_inc:
        # Execute node
        await select_tool_categories_node(state)
        
        # Verify metrics incremented
        mock_inc.assert_called_with("simple_tool_retry_same_tool")


@pytest.mark.asyncio
async def test_retry_updates_cached_planner_plan():
    """Verify that cached planner_plan is updated with retry parameters.
    
    This is CRITICAL for retry functionality: The tool_execution node
    reuses the cached planner_plan, so we must update it with the new
    parameters from failure reflection. Otherwise, the old faulty
    parameters will be used again!
    """
    # State with cached planner_plan and reflection suggesting param change
    state = {
        "facts": {
            "task_id": 1,
            "message": "Run nmap with -sn and -p 80",
            "selected_tool": "information_gathering.network_discovery.nmap",
            "tool_parameters": {
                "information_gathering.network_discovery.nmap": {
                    "target": "127.0.0.1",
                    "ports": "80",
                    "scan_types": ["-sn"]  # Faulty param
                }
            },
            "metadata": {
                "reflection_suggestions": {
                    "alternative_tool": None,
                    "same_tool_retry": True,
                    "suggested_params": {
                        "scan_types": ["-sS"]  # Corrected param
                    },
                    "reasoning": "Cannot use -p with -sn, switch to -sS",
                    "can_recover": True,
                    "failure_category": "invalid_params"
                },
                "retry_tracking": {"count": 1},
                "planner_plan": {  # Cached plan with OLD params
                    "selected_tools": ["information_gathering.network_discovery.nmap"],
                    "tool_parameters": {
                        "information_gathering.network_discovery.nmap": {
                            "target": "127.0.0.1",
                            "ports": "80",
                            "scan_types": ["-sn"]  # OLD faulty param
                        }
                    },
                    "execution_strategy": "sequential",
                    "reasoning": "Initial plan",
                    "expected_outcome": "Scan results"
                }
            }
        },
        "trace": {
            "history": [],
            "reasoning": [],
        }
    }
    
    # Execute the node (retry path should clear cached plan and emit guidance)
    result = await select_tool_categories_node(state)
    
    # Verify facts were not updated with corrected executable params
    updated_facts = result["facts"]
    tool_params = updated_facts["tool_parameters"]["information_gathering.network_discovery.nmap"]
    assert tool_params["scan_types"] == ["-sn"], "Tool parameters should not be directly corrected"
    assert tool_params["ports"] == "80", "Original ports param should be preserved"
    assert tool_params["target"] == "127.0.0.1", "Original target should be preserved"
    
    # CRITICAL: Verify cached planner_plan was cleared so the builder replans
    updated_metadata = updated_facts["metadata"]
    updated_plan = updated_metadata.get("planner_plan")
    assert updated_plan is None, "Cached planner_plan should be cleared for fresh builder output"
    assert "-sS" in updated_metadata["next_tool_hint"]
    
    # Verify reflection suggestions were cleared
    assert "reflection_suggestions" not in updated_metadata
