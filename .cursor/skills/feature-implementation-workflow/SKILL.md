---
name: feature-implementation-workflow
description: Runs DrowAI's state-driven feature implementation workflow from `.cursor/agents/implementation-state.md`. Use when the user says implement, implement-this, continue implementation, run the implementation guide, advance to next task, or wants the existing feature-implementer plus review-loop workflow to continue automatically until completion or a hard stop.
---

# Feature Implementation Workflow

Use this skill to run the current DrowAI implementation automation flow.

This skill preserves the existing working behavior:
- `@feature-implementer` implements exactly one guide task.
- `implementation-review-loop` performs phase-gated review/fix through the state ledger.
- The main agent advances tasks continuously within a phase, then gates phase transition on review-state `COMPLETE`.
- The loop continues until the guide is complete, `MAX_ROUNDS_REACHED`, `NEEDS_CLARIFICATION`, or the user stops.

## Durable Files

- `.cursor/agents/implementation-state.md` — current guide, phase, task, intent, `advance_after_complete`, and ownership checklist.
- `.cursor/agents/implementation-review-state.md` — current task blocker ledger and review-loop status.
- `.cursor/agents/IMPLEMENTATION_FLOW.md` — detailed orchestration reference.

## Trigger Examples

- "implement this"
- "continue implementation"
- "run implementation workflow"
- "go next"
- "implement from state"
- Command: `implement-this`

## Workflow

1. Read `.cursor/agents/implementation-state.md`.
2. If the user named a guide/phase/task, pass that to `@feature-implementer`; otherwise call `@feature-implementer` with the current state.
3. Let `@feature-implementer` implement one task and run verification.
4. Determine whether the next task remains in the same phase:
   - If yes, call `@feature-implementer` with `next` and continue implementation without review.
   - If no (phase boundary reached), initialize `.cursor/agents/implementation-review-state.md` with `mode: current_phase`, current `phase`, `task: ""`, and `status: READY_FOR_REVIEW`.
5. At phase boundary, invoke `implementation-review-loop` in Current Phase Review mode.
6. Route by review-state:
   - `COMPLETE`: call `@feature-implementer` with `next` to start the next phase (if any), else stop.
   - `REVIEW_BLOCKED`: continue the phase review-loop; do not manually paste reports.
   - `READY_FOR_REVIEW`: call a fresh reviewer through the review-loop skill.
   - `NEEDS_CLARIFICATION`: stop and ask for missing input from review-state.
   - `MAX_ROUNDS_REACHED`: stop and ask for a human decision using review-state.
7. Repeat until the guide has no next task or a hard stop status is reached.

## Hard Rules

- Do not ask the user whether to call the next agent when state has a clear next transition.
- Do not paste full reports between agents; state files are authoritative.
- Do not skip the review-loop completion gate.
- Do not call `@feature-implementer next` after `MAX_ROUNDS_REACHED` or `NEEDS_CLARIFICATION`.
- Keep implementation task-scoped; one feature-implementer invocation equals one guide task.
- Phase transitions must be gated by Current Phase Review `COMPLETE`.
- If state files conflict, resolve or ask before continuing.

## Final Response

When the workflow stops, report:
- final status from `.cursor/agents/implementation-review-state.md`,
- current `guide`, `phase`, and `task`,
- verification summary if available,
- whether the guide completed or why the loop stopped.
