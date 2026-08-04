<!-- Purpose: Record removal of the test-only post-tool reasoning decision path. -->

# Post-Tool Reasoning Test Compatibility Seam

**Status:** Complete

**Implementation commit:** `24bb152` (`refactor(ptr): remove the test-only decision path`)

## Completed boundary

Wired post-tool reasoning now has one provider-native path: visible observation
text plus one internal route-tool decision. Tests that previously monkeypatched
`node.analyze_tool_result` now use the existing fake `LLMClient` and streaming
adapter seams. Direct analysis-helper coverage remains in its core module.

The `_DEFAULT_ANALYZE_TOOL_RESULT` identity branch, duplicated streaming and
non-streaming execution, and its unused production import were removed.

## Preserved contracts

- Streaming event order and usage attribution are unchanged.
- Refusal handling, retry behavior, route recovery, state updates, and
  parent-handoff behavior are unchanged.
- No second articulation or runtime decision call was introduced.

## Verification evidence

- PTR failure-detection, prompt-context, core, streaming, route-recovery,
  event-order, simple-tool, and parent-handoff suites passed.
- Source searches found no default-analysis identity branch or monkeypatch-
  driven callable identity check in production code.
