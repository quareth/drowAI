---
name: feature-implementation-workflow
description: Run DrowAI's repo-local state-driven feature implementation workflow from `.codex/agents/implementation-state.md`. Use when the user says implement, implement-this, continue implementation, run the implementation guide, advance to next task, go next, or wants the existing feature-implementer plus implementation-review-loop workflow to continue automatically until completion or a hard stop.
---

# Feature Implementation Workflow

Use this skill to run the current DrowAI implementation automation flow through Codex agents and repo-local state files.

This skill preserves the state-driven behavior:
- `feature-implementer` implements exactly one guide task.
- `implementation-review-loop` performs phase-gated review/fix through the review-state ledger.
- The main agent advances tasks continuously within a phase, then gates phase transition on review-state `COMPLETE`.
- The loop continues until the guide is complete, `MAX_ROUNDS_REACHED`, `NEEDS_CLARIFICATION`, or the user stops.

## Durable Files

- `.codex/agents/implementation-state.md` - current guide, phase, task, intent, `advance_after_complete`, and ownership checklist.
- `.codex/agents/implementation-review-state.md` - current task blocker ledger and review-loop status.
- `.codex/agents/IMPLEMENTATION_FLOW.toml` - detailed orchestration reference for manual recovery.

## Trigger Examples

- "implement this"
- "continue implementation"
- "run implementation workflow"
- "go next"
- "implement from state"
- Command: `implement-this`

## Workflow

1. Read `.codex/agents/implementation-state.md`.
2. If the user named a guide, phase, or task, pass that scope to `feature-implementer`; otherwise call `feature-implementer` with the current state.
3. Let `feature-implementer` implement one task and run verification.
4. Determine whether the next task remains in the same phase:
   - If yes, call `feature-implementer` with `next` and continue implementation without review.
   - If no (phase boundary reached), initialize `.codex/agents/implementation-review-state.md` with `mode: current_phase`, current `phase`, `task: ""`, and `status: READY_FOR_REVIEW`.
5. At phase boundary, invoke `implementation-review-loop` in Current Phase Review mode.
6. Route by review-state:
   - `COMPLETE`: call `feature-implementer` with `next` to start the next phase task (if any), else stop.
   - `REVIEW_BLOCKED`: continue the phase review-loop; do not manually paste reports.
   - `READY_FOR_REVIEW`: call a fresh reviewer through the review-loop skill.
   - `NEEDS_CLARIFICATION`: stop and ask for missing input from review-state.
   - `MAX_ROUNDS_REACHED`: stop and ask for a human decision using review-state.
7. Repeat until the guide has no next task or a hard stop status is reached.

## Hard Rules

- Do not ask the user whether to call the next agent when state has a clear next transition.
- Do not paste full reports between agents; state files are authoritative.
- Do not skip the review-loop completion gate.
- Do not call `feature-implementer next` after `MAX_ROUNDS_REACHED` or `NEEDS_CLARIFICATION`.
- Keep implementation task-scoped; one `feature-implementer` invocation equals one guide task.
- Phase transitions must be gated by Current Phase Review `COMPLETE`.
- If state files conflict, resolve or ask before continuing.

## Refactor Guide Overlay

When the active guide is under `docs/refactor/` or references `docs/refactor/RULES.md`:

- Treat `docs/refactor/RULES.md` as binding over the guide.
- Do not begin structural extraction until every guide-defined stabilization and baseline phase is review-complete.
- The structural program as a whole must follow extract beside legacy -> prove intact -> migrate references -> remove legacy -> rerun locked tests.
- Do not allow fallback paths, compatibility shims, re-exports, aliases, or new feature flags as implementation shortcuts.
- A guide-explicit extraction/proof phase may temporarily duplicate source definitions while the legacy path remains canonical, untouched, and the only production path. Review that phase for direct-test equivalence and absence of caller migration.
- Removal and final review phases must verify no scoped legacy/dead code or duplicate definitions remain.
- Final review must include the guide's Review & Cleanup and P0 comparison gates.

## Final Response

When the workflow stops, report:
- final status from `.codex/agents/implementation-review-state.md`,
- current `guide`, `phase`, and `task`,
- verification summary if available,
- whether the guide completed or why the loop stopped.
