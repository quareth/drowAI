# Post-Tool Reasoning Streaming Adapters

This package provides capability-specific streaming adapters for post-tool reasoning.

## Architecture

The streaming layer uses the **Adapter Pattern** to handle capability-specific streaming without polluting core logic with `if capability ==` checks.

### Components

#### 1. Base Adapter (`base.py`)

Abstract interface that all streaming adapters must implement.

**Key Methods:**
- `stream_observation(writer, llm_client, system_prompt, user_prompt, conversation_id, turn_id, sequence)` - Stream LLM response and return parsed output
- `get_stream_identifiers(interactive, config)` - Get capability-appropriate stream identifiers

#### 2. DR Streaming Adapter (`dr_adapter.py`)

Streaming adapter for `deep_reasoning` capability.

**Features:**
- Uses `observation_*` events for frontend display
- Tracks DR iterations via `derive_dr_stream_identifiers`
- Returns tuple of (conversation_id, turn_id, dr_iteration)

#### 3. Simple Streaming Adapter (`simple_adapter.py`)

Streaming adapter for `simple_tool_execution` capability.

**Features:**
- Uses `observation_*` events for frontend display
- Simple conversation/turn tracking via `derive_stream_identifiers`
- Returns tuple of (conversation_id, turn_id)

#### 4. Streaming Adapter Factory (`factory.py`)

Factory for creating the appropriate adapter based on capability.

**Usage:**
```python
from agent.graph.nodes.post_tool_reasoning.streaming import StreamingAdapterFactory

adapter = StreamingAdapterFactory.create("deep_reasoning")
# or
adapter = StreamingAdapterFactory.create("simple_tool_execution")
```

## Design Principles

1. **Separation of Concerns**: Streaming logic is separate from core reasoning logic
2. **Capability-Specific**: Each adapter handles streaming for its capability
3. **Consistent Interface**: All adapters implement the same base interface
4. **Factory Pattern**: Centralized adapter creation

## Usage Example

```python
from agent.graph.nodes.post_tool_reasoning.streaming import StreamingAdapterFactory

# Create adapter for capability
capability = state.facts.capability or "simple_tool_execution"
adapter = StreamingAdapterFactory.create(capability)

# Get stream identifiers (capability-specific)
stream_ids = adapter.get_stream_identifiers(interactive, config)
conversation_id = stream_ids[0]
turn_id = stream_ids[1]

# Stream observation (capability-specific events)
output, streamed = await adapter.stream_observation(
    writer=writer,
    llm_client=llm_client,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    conversation_id=conversation_id,
    turn_id=turn_id,
    sequence=turn_sequence,
)
```

## Event Flow

Both adapters emit the same event types but with capability-specific identifiers:

1. `observation_start` - Signals beginning of observation
2. `observation_delta` - Streams observation text chunks when plain-text observation streaming is used
3. `observation_snapshot` - Final clean observation text
4. `observation_section_end` - Signals completion

The key difference is in the stream identifiers:
- **DR**: Uses iteration tracking for multi-step reasoning
- **Simple**: Uses simple conversation/turn tracking

## Testing

Unit tests are located in `streaming/tests/`:
- `test_factory.py` - 6 tests for factory pattern
- `test_adapters.py` - 6 tests for adapter implementations

Run tests with:
```bash
pytest agent/graph/nodes/post_tool_reasoning/streaming/tests/ -v
```

## Adding New Capabilities

To add a new capability:

1. Create a new adapter class inheriting from `StreamingAdapter`
2. Implement `stream_observation` and `get_stream_identifiers`
3. Register in `StreamingAdapterFactory.create()`
4. Add tests in `streaming/tests/`

Example:
```python
class NewCapabilityAdapter(StreamingAdapter):
    async def stream_observation(self, writer, llm_client, ...):
        # Implement capability-specific streaming
        pass
    
    def get_stream_identifiers(self, interactive, config):
        # Return capability-specific identifiers
        pass
```

