# implementation-review-state.example.md

Template for `.cursor/agents/implementation-review-state.md`. This file is a current-cycle blocker ledger, not long-term review memory.

Copy the clean YAML block into `.cursor/agents/implementation-review-state.md` when starting a review loop. Keep `.cursor/agents/implementation-state.md` as the source of truth for guide, phase, task, intent, and ownership checklist.

## Clean State

Use this before every fresh reviewer run, including after the fixer applies changes:

```yaml
schema_version: 2
mode: "current_task" # current_task | current_phase | final_implementation
status: READY_FOR_REVIEW
round: 0
max_rounds: 20 # fixed hard cap only; round is audit history, not a reviewer-chosen limit
implementation_state: ".cursor/agents/implementation-state.md"
guide: "docs/path/to/implementation-guide.md"
related_design: "docs/path/to/design.md"
phase: "0" # required for current_task/current_phase; use "" for final_implementation
task: "0.1" # required only for current_task; use "" for current_phase/final_implementation
scope_summary: "Current task, current phase, or full implementation review against the guide."
intent_summary: "Short implementation intent from implementation-state.md."
last_actor: "feature-implementer"
updated_at: "YYYY-MM-DDTHH:MM:SSZ"

fresh_review_policy:
  required_after_fix: true
  active_findings_cleared_before_review: true
  reviewer_must_review_full_scope_each_round: true
  spawn_new_reviewer_agent_each_cycle: true
  no_prior_review_context_for_reviewer: true

stop_conditions:
  no_active_blockers: false
  max_rounds_reached: false
  needs_clarification: false

active_findings: []
```

## Blocked State

Reviewer writes current-cycle findings only:

```yaml
status: REVIEW_BLOCKED
round: 1
active_findings:
  - id: "R1-P1"
    round: 1
    priority: "P1"
    severity: "blocker"
    category: "security_boundary"
    title: "The Phase 2 test spec can accidentally authorize cross-user task access."
    status: "open"
    location:
      document: "docs/path/to/implementation-guide.md"
      section: "Phase 2 / Tests"
      lines: "633"
    problem: "Literal same-tenant authorization wording permits any default-tenant user to access another user's task."
    evidence:
      guide:
        - "Guide line 585 preserves Task.user_id == current_user.id checks."
        - "Guide line 633 says same-tenant task is accepted."
      code:
        - "backend/services/task/access_service.py:15 enforces user-owned task access."
      design:
        - "HLD line 62 requires user authorization before runtime access."
      tests:
        - "No test currently covers same tenant + different user denial."
    why_it_blocks: "It changes the security boundary and would produce tests that bless cross-user access."
    required_fix: "Change the test requirement to cover same tenant + same user allow, same tenant + different user deny, and foreign tenant deny."
    fixer_notes: ""
```

After the fixer applies changes, it must return the file to the clean state shape with:

- `status: READY_FOR_REVIEW`
- the same scope metadata
- the same/incremented `round` audit counter
- `active_findings: []`
- no archived findings
- no rounds list
- no fix attempts

For final implementation review, use:

```yaml
mode: "final_implementation"
phase: ""
task: ""
scope_summary: "Full implementation review against the guide."
```

For current phase review, use:

```yaml
mode: "current_phase"
phase: "2"
task: ""
scope_summary: "Current phase review against all tasks and acceptance criteria in Phase 2."
```

## Status Values

- `READY_FOR_REVIEW`: a fresh reviewer subagent should perform a new review with no prior review context.
- `REVIEW_BLOCKED`: latest fresh review found current active blockers/majors; fixer should read `active_findings`.
- `COMPLETE`: latest fresh review found no blockers and scope is ready.
- `NEEDS_CLARIFICATION`: reviewer or fixer cannot proceed without missing input.
- `MAX_ROUNDS_REACHED`: review loop hit the fixed hard cap of 20 rounds; stop for human decision.

## Rules

1. `active_findings` contains only current-cycle findings.
2. Each finding must be detailed enough for the fixer to act without chat context.
3. Fixer reads only `active_findings` for required fixes.
4. After fixer runs, it clears `active_findings` and resets status to `READY_FOR_REVIEW`.
5. Do not preserve `archived_findings`, `rounds`, `fix_attempts`, previous reports, or fixer summaries in this file.
6. The next reviewer must be a fresh subagent and must rediscover any still-active blocker from guide/code/design evidence.
7. `round` is recordkeeping only. Do not stop because of round count unless the fixed hard cap of 20 is reached.
