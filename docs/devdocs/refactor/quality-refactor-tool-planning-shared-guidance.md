<!-- Purpose: Record the completed structural composition of shared native-tool prompt guidance. -->

# Shared Native-Tool Guidance Composition

**Status:** Complete

**Implementation commit:** `c005e9d` (`refactor(prompts): compose shared native-tool guidance`)

## Completed boundary

The tool-planning prompt family now owns a versioned
`native_tool_call_guidance.txt` component in `v7`. `TemplateLoader` resolves the
component structurally, and the existing tool-planning builder renders it with:

- caller-specific call requirements;
- optional selector-strategy guidance;
- the existing batch limit.

The main planner supplies mandatory-call wording and selector guidance. The
subagent variant supplies conditional-call wording without the selector
paragraph. Historical `v6` assets and the subagent-runtime prompt family remain
unchanged. Prose slicing, literal heading searches, and prefix replacement were
removed.

## Preserved contracts

- Main and subagent system prompts are byte-for-byte unchanged.
- Prompt ordering, newlines, batch-limit substitution, tool schemas, and model
  call behavior are unchanged.
- `latest.txt` now selects the complete `v7` directory; historical prompt
  versions remain immutable.

## Verification evidence

- Byte-for-byte golden checks passed for both main and subagent prompts.
- Tool-planning and subagent-runtime builder suites passed as part of a
  217-test focused prompt gate.
- Structural lookup tests passed with surrounding planner prose varied.
- The `v7` asset family was checked for completeness, and source searches found
  no remaining prompt-substring extraction in this path.
