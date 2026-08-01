# Post-Tool Reasoning Streaming

This package streams the parent agent's visible post-action observation while
collecting the matching internal `ptr_commit` call from the same model turn.

## Components

- `base.py` owns the provider-neutral stream, event lifecycle, usage capture,
  and commit-only recovery when a provider truncates or omits `ptr_commit`.
- `dr_adapter.py` and `simple_adapter.py` supply capability-specific usage and
  logging labels.
- `factory.py` selects the adapter for `deep_reasoning` or
  `simple_tool_execution`.

The wired path calls `StreamingAdapter.stream_observation_with_route()`. It
emits `observation_start`, zero or more `observation_delta` events, one final
`observation_snapshot`, and `observation_section_end`. Partial function
arguments are never emitted as visible observation text.

## Tests

Run the focused factory and PTR integration coverage with:

```bash
pytest agent/graph/nodes/post_tool_reasoning/streaming/tests \
  agent/graph/nodes/post_tool_reasoning/tests \
  agent/graph/tests/test_post_tool_reasoning_phase3_split.py
```
