# Post-Tool Reasoning Core Logic

This package contains capability-agnostic, pure-function implementations of post-tool reasoning logic.

## Architecture

The core logic is organized into three main modules:

### 1. Failure Detection (`failure_detection.py`)

Pure functions for detecting and classifying tool failures.

**Key Functions:**
- `detect_failure(context: FailureContext) -> Tuple[bool, Optional[str]]` - Detects if a tool failed and classifies the failure type
- `classify_failure_category(stderr: str, exit_code: Optional[int]) -> str` - Classifies failures into categories (network_error, permission_denied, timeout, etc.)
- `build_failure_context_from_state(state: InteractiveState) -> FailureContext` - Extracts failure context from state

**Failure Categories:**
- `network_error` - Connection refused, network unreachable
- `permission_denied` - Permission errors
- `timeout` - Tool timed out
- `tool_unavailable` - Tool not found
- `invalid_params` - Invalid parameters
- `empty_output` - No output produced
- `unknown` - Cannot determine cause

### 2. Retry Logic (`retry_logic.py`)

Pure functions for managing retry attempts.

**Key Functions:**
- `get_retry_count(metadata: Dict) -> int` - Extracts retry count from metadata
- `can_retry(retry_count: int, max_retries: int) -> bool` - Checks if retry budget is available
- `increment_retry_count(metadata: Dict) -> Dict` - Returns new metadata with incremented retry count (pure function, no mutation)

**Constants:**
- `MAX_RETRIES = 2` - Maximum retry attempts allowed
- `RETRY_METADATA_KEY = "retry_tracking"` - Metadata key for retry tracking

### 3. LLM Analysis (`llm_analysis.py`)

Capability-agnostic LLM interaction for analyzing tool results.

**Key Functions:**
- `analyze_tool_result(llm_client, system_prompt, user_prompt, failure_context) -> PostToolReasoningDecisionOutput` - Makes non-streaming LLM call to return decision-only structured payload
- `build_analysis_context(failure_detected, failure_category, retry_count, max_retries) -> dict` - Builds context dictionary for LLM prompts

**Constants:**
- `MAX_REASONING_TOKENS = 500` - Token limit for LLM responses
- `DEFAULT_TEMPERATURE = 0.3` - Temperature for LLM calls

## Design Principles

1. **Pure Functions**: All core logic functions are pure - no side effects, no state mutation
2. **Capability Agnostic**: No `if capability ==` checks - works for any capability
3. **Testable**: Easy to unit test with simple inputs/outputs
4. **Composable**: Functions can be composed and reused

## Usage Example

```python
from agent.graph.nodes.post_tool_reasoning.core import (
    build_failure_context_from_state,
    detect_failure,
    get_retry_count,
    can_retry,
    increment_retry_count,
)

# Detect failure
failure_ctx = build_failure_context_from_state(state)
failure_detected, category = detect_failure(failure_ctx)

if failure_detected:
    # Check retry budget
    metadata = state.facts.safe_metadata
    retry_count = get_retry_count(metadata)
    
    if can_retry(retry_count):
        # Increment retry count (returns new dict, doesn't mutate)
        new_metadata = increment_retry_count(metadata)
        state.facts.metadata = new_metadata
```

## Testing

Comprehensive unit tests are located in `core/tests/`:
- `test_failure_detection.py` - 17 tests covering all failure scenarios
- `test_retry_logic.py` - 13 tests covering retry logic
- `test_llm_analysis.py` - 12 tests with mocked LLM clients

Run tests with:
```bash
pytest agent/graph/nodes/post_tool_reasoning/core/tests/ -v
```

All tests use pure functions with no external dependencies, making them fast and reliable.
