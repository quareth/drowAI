import pytest

pytest.importorskip("langgraph")

from agent.graph import (  # noqa: E402  # pylint: disable=wrong-import-position
    InteractiveInput,
    InteractiveState,
    build_initial_state,
    build_minimal_interactive_graph,
    get_compiled_minimal_graph,
    get_default_checkpointer,
)


def test_interactive_input_to_state():
    payload = InteractiveInput(task_id=42, message="hello", conversation_id="conv-1")
    state = payload.to_state()
    assert state.facts.task_id == 42
    assert state.facts.message == "hello"
    assert state.facts.conversation_id == "conv-1"
    assert state.trace.final_text is None
    assert state.facts.tool_ids == []


def test_build_initial_state_dump():
    payload = InteractiveInput(task_id=7, message="ping")
    state_dict = build_initial_state(payload)
    assert state_dict["facts"]["task_id"] == 7
    assert state_dict["facts"]["message"] == "ping"
    assert state_dict["trace"]["final_text"] is None


def test_minimal_graph_sets_defaults():
    payload = InteractiveInput(task_id=9, message="do something", conversation_id="c-9")
    state = payload.to_state()
    state.trace.final_text = "ok"
    graph = build_minimal_interactive_graph(checkpointer=get_default_checkpointer())
    result = graph.invoke(
        state.as_graph_state(),
        config={"configurable": {"thread_id": "test-thread"}},
    )
    interactive_state = InteractiveState.from_mapping(result)
    assert interactive_state.trace.final_text == "ok"
    assert interactive_state.facts.capability == "respond_only"
    assert any(
        "respond_only" in entry.lower()
        for entry in interactive_state.trace.reasoning
    )


def test_checkpointer_singleton():
    cp1 = get_default_checkpointer()
    cp2 = get_default_checkpointer()
    assert cp1 is cp2


def test_cached_graph_reuses_instance():
    g1 = get_compiled_minimal_graph()
    g2 = get_compiled_minimal_graph()
    assert g1 is g2
