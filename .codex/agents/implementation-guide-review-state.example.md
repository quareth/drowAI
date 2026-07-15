# implementation-guide-review-state.example.md

Template for `.codex/agents/implementation-guide-review-state.md`.

This file is a current-cycle blocker ledger for implementation-guide review, not for code changes.

```yaml
schema_version: 1
mode: "full_guide" # full_guide | section | current_phase
status: READY_FOR_REVIEW
round: 0
max_rounds: 20
guide_state: ".codex/agents/implementation-guide-state.md"
guide: "docs/path/to/implementation-guide.md"
related_design: "docs/path/to/high-level-design.md"
phase: "" # required when mode=current_phase
scope_summary: "Full implementation guide review."
intent_summary: "Guide review goal."
last_actor: "main-agent"
updated_at: "YYYY-MM-DDTHH:MM:SSZ"
review_policy:
  blocker_only: true
  ignore_enhancements: true
  spawn_new_reviewer_agent_each_cycle: true
  no_prior_review_context_for_reviewer: true
  preserve_guide_structure: true
stop_conditions:
  no_active_blockers: false
  max_rounds_reached: false
  needs_clarification: false
active_findings: []
```

For current phase guide review, use:

```yaml
mode: "current_phase"
phase: "1"
scope_summary: "Current phase guide review against all tasks and acceptance criteria in Phase 1."
```

`active_findings` shape:

```yaml
active_findings:
  - id: "R1-P1"
    round: 1
    priority: "P1"
    severity: "blocker"
    category: "contract_contradiction"
    title: "Guide section contradicts security boundary."
    status: "open"
    location:
      section: "Phase 2 / Tests"
      lines: "633"
    problem: "Statement permits same-tenant cross-user access."
    evidence:
      guide:
        - "Section A says ownership checks required."
        - "Section B allows same-tenant access."
      design:
        - "HLD requires user authorization before runtime access."
      code:
        - "access_service enforces user-owned access."
    why_it_blocks: "Would produce unsafe implementation and incorrect test expectations."
    required_fix: "Specify same user allow; same tenant different user deny; foreign tenant deny."
```
