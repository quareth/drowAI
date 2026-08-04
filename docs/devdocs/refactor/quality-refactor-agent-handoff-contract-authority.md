<!-- Purpose: Record the completed consolidation of the agent-handoff contract authority. -->

# Agent Handoff Contract Authority

**Status:** Complete

**Implementation commit:** `bca203f` (`refactor(subagents): centralize the handoff contract`)

## Completed boundary

`agent/subagents/handoff.py` is the single domain authority for:

- `AgentHandoffEntry` shape validation;
- graph/backend single-entry normalization;
- ordered collection filtering, first-occurrence deduplication, bounds, and
  strict rejection;
- the strict handoff JSON-schema fragment, including registry-scoped agent
  enumeration.

PTR models re-export the canonical model, decision routing delegates entry
normalization, backend ownership policy retains only routing and error
translation, and structured LLM schemas consume the canonical fragment. The
displaced local implementations were removed.

## Preserved contracts

- The three-field shape and `"required"` marker are unchanged.
- Authored Pydantic values remain preserved where they were preserved before.
- Trim/lowercase normalization remains limited to the graph/backend boundaries.
- Input ordering, first-occurrence deduplication, bounds, and caller-specific
  strict versus best-effort behavior are unchanged.
- Dispatch, registry lookup, and backend orchestration remain outside the
  domain contract module.

## Verification evidence

- `tests/subagents/test_handoff.py` covers malformed, blank, extra-field,
  normalized, duplicate, ordered, bounded, strict, and best-effort inputs.
- PTR model/route, decision-router, ownership-policy, classifier,
  parent-control, and structured-schema suites passed.
- The generated test inventory was updated and verified.
- Source searches found no displaced handoff normalizer or schema builder in
  migrated callers.
