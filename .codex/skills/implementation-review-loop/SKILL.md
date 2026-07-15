---
name: implementation-review-loop
description: Run DrowAI's repo-local state-driven implementation review loop through `.codex/agents/implementation-review-state.md`. Use when the user asks to review current implementation work, review a phase/task, run final implementation review, run review-and-fix until no blockers, or mentions implementation-review-state, blocker ledger, reviewer/fixer loop, or fresh full review.
---

# Implementation Review Loop

Use this skill to run DrowAI's automated implementation review loop through state files and fresh Codex subagents.

The durable files are:
- `.codex/agents/implementation-state.md` - guide, intent, ownership checklist, and optional current phase/task for feature-implementer workflow.
- `.codex/agents/implementation-review-state.md` - current-cycle blocker ledger and neutral scope metadata.

`max_rounds` is always the fixed hard cap `20`. Normalize it back to `20` if a state file has another value. `round` is only a record of fresh review passes; it must not be used to stop early before the hard cap.

## New-Chat Simulation Rule

Each review pass must simulate this manual flow:

```text
new chat -> guide + review prompt -> blocker report -> fix -> new chat -> guide + review prompt
```

To preserve that behavior:
- Spawn a fresh `implementation-reviewer` subagent for every review pass. Do not resume or reuse a previous reviewer agent.
- Do not pass prior reviewer reports, fixer summaries, active findings, archived findings, or chat history to the reviewer.
- Before calling the next reviewer after a fix, ensure `.codex/agents/implementation-review-state.md` has no previous findings, no fix attempts, and no review history.
- The next reviewer may read only neutral scope metadata: mode, guide, related design, optional phase/task, scope summary, intent summary, round counter, hard cap, and empty `active_findings`.
- The fixer may read `active_findings` while fixing, but must clear them before the next reviewer run.

## Modes

### 1. Current Task Review

Use when feature implementation is in progress or has just completed one phase/task.

Trigger examples:
- "review current task"
- "review this implementation phase"
- "run task review loop"
- Command: `review-current-task`

Scope:
- Review only the current `guide`, `phase`, and `task` from `.codex/agents/implementation-state.md`.
- If a guide defines phase-level acceptance, include that phase only.
- Continue reviewer -> fixer -> fresh reviewer until `COMPLETE`, `MAX_ROUNDS_REACHED`, or `NEEDS_CLARIFICATION`.

### 2. Current Phase Review

Use when implementation tasks for the active phase are complete and you want one review/fix loop for the whole phase before moving to the next phase.

Trigger examples:
- "review current phase"
- "run phase review loop"
- "phase gate review"
- Command: `review-current-phase`

Scope:
- Review all tasks and phase acceptance criteria for the current `phase` from `.codex/agents/implementation-state.md`.
- Initialize review-state with `mode: current_phase`, keep `phase`, set `task: ""`, and a phase-scope summary.
- Continue reviewer -> fixer -> fresh reviewer until `COMPLETE`, `MAX_ROUNDS_REACHED`, or `NEEDS_CLARIFICATION`.

### 3. Final Implementation Review

Use when implementation is complete and the user wants a standalone review-and-fix loop without advancing tasks.

Trigger examples:
- "final implementation review"
- "review whole implementation until no blockers"
- "run final review loop"
- Command: `review-final-implementation`

Scope:
- Review the full guide referenced by `.codex/agents/implementation-state.md`, unless the user names a different guide or scope.
- Ignore `phase` and `task` in `.codex/agents/implementation-state.md`; those are only for feature-implementer/current-task review.
- Initialize review-state with `mode: final_implementation`, `phase: ""`, `task: ""`, and `scope_summary: "Full implementation review against the guide."`
- Do not call `feature-implementer`.
- Continue reviewer -> fixer -> fresh reviewer until no blockers remain or the hard cap of 20 rounds is reached.

## Required Behavior

1. Read `.codex/agents/implementation-state.md`.
2. Initialize or reset `.codex/agents/implementation-review-state.md` for the selected mode with clean `active_findings: []`.
3. Call a fresh `implementation-reviewer` subagent.
4. If review-state becomes `REVIEW_BLOCKED`, call `implementation-fixer`.
5. After fixer applies fixes, it must reset review-state to `READY_FOR_REVIEW` with clean `active_findings: []`; then call a fresh `implementation-reviewer` subagent with no pasted context.
6. Stop only on `COMPLETE`, `MAX_ROUNDS_REACHED`, or `NEEDS_CLARIFICATION`; do not stop because a reviewer chose a lower round count.

## Fresh Review Rule

After every fixer run, the next reviewer must perform a complete fresh review of the scoped implementation. It must not know or verify the previous fix.

The review-state must not preserve prior review history visible to the reviewer:
- Clear `active_findings`.
- Do not keep `archived_findings`, `rounds`, or `fix_attempts` in review-state.
- Keep only `round` as a numeric audit counter.
- Reviewer repopulates `active_findings` from a fresh review.

## Finding Shape

Every active finding must be detailed enough for the fixer to act without chat context:

```yaml
active_findings:
  - id: "R2-P1"
    priority: "P1"
    severity: "blocker"
    category: "security_boundary"
    title: "The Phase 2 test spec can accidentally authorize cross-user task access."
    location:
      document: "docs/path/to/guide.md"
      section: "Phase 2 / Tests"
      lines: "633"
    problem: "Literal same-tenant authorization wording permits one default-tenant user to access another user's task."
    evidence:
      guide:
        - "Guide line 585 preserves Task.user_id == current_user.id checks."
        - "Guide line 633 says same-tenant task is accepted."
      code:
        - "backend/services/task/access_service.py:15 enforces user-owned task access."
      design:
        - "HLD line 62 requires user authorization before runtime access."
    why_it_blocks: "It changes the security boundary and would produce tests that bless cross-user access."
    required_fix: "Change the test requirement to cover same tenant + same user allow, same tenant + different user deny, foreign tenant deny."
    fixer_notes: ""
```

## State Status Routing

- `READY_FOR_REVIEW` -> call `implementation-reviewer`
- `REVIEW_BLOCKED` -> call `implementation-fixer`
- `FIX_APPLIED` -> invalid steady state; fixer should reset state to `READY_FOR_REVIEW` before handoff
- `COMPLETE` -> stop; in feature implementation workflow, main agent may continue with `feature-implementer next`
- `NEEDS_CLARIFICATION` -> stop and ask for the missing input recorded in review-state
- `MAX_ROUNDS_REACHED` -> stop and ask for human decision; this is valid only at the fixed hard cap of 20 rounds

## Hard Rules

- Do not paste full reports between agents. State files are authoritative.
- Do not let reviewer directly fix.
- Do not let fixer broaden scope beyond `active_findings`.
- Do not preserve stale active findings after a fix; clear them and force a new reviewer agent.
- Do not preserve archived findings or fix attempts in review-state.
- Do not call `feature-implementer` in final implementation review mode.
