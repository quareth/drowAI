---
name: implementation-guide-review-loop
description: Run a state-driven review/fix loop for implementation-guide documents only (not code), using `.cursor/agents/implementation-guide-state.md` and `.cursor/agents/implementation-guide-review-state.md`. Use when the user asks to review an implementation guide, run blocker-only guide review, or loop reviewer/fixer until guide blockers are resolved.
---

# Implementation Guide Review Loop

Use this skill for implementation-guide document quality loops, not code implementation loops.

Durable files:
- `.cursor/agents/implementation-guide-state.md` - guide path and review scope.
- `.cursor/agents/implementation-guide-review-state.md` - current-cycle blocker ledger.

## New-Chat Simulation Rule

Each cycle must simulate:

```text
new chat -> guide review -> blocker findings -> guide fix -> new chat -> guide review
```

Rules:
- Spawn a fresh `@implementation-guide-reviewer` every review cycle.
- Do not pass prior reviewer/fixer chat content to the next reviewer.
- Fixer must reset guide-review-state to clean `READY_FOR_REVIEW` after applying guide fixes.

## Modes

1. `full_guide`:
- Review the entire guide from guide-state.

2. `section`:
- Review only the section selected in guide-state.

## Loop behavior

1. Read `.cursor/agents/implementation-guide-state.md`.
2. Initialize/reset `.cursor/agents/implementation-guide-review-state.md` with clean `active_findings: []`.
3. Call fresh `@implementation-guide-reviewer`.
4. If state -> `REVIEW_BLOCKED`, call `@implementation-guide-fixer`.
5. Fixer edits the guide and resets state to `READY_FOR_REVIEW`.
6. Call a new `@implementation-guide-reviewer`.
7. Repeat until:
   - `COMPLETE`, or
   - `MAX_ROUNDS_REACHED` (hard cap 20), or
   - `NEEDS_CLARIFICATION`.

## Hard rules

- Blocker-only findings; ignore enhancements.
- Fixer edits guide docs only (plus guide-review-state reset).
- Do not modify application code in this loop.
- Keep guide structure stable unless blocker resolution requires structural change.
