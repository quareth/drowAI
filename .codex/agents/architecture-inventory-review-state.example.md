# architecture-inventory-review-state.example.md

Template for `.codex/agents/architecture-inventory-review-state.md`.

This file is a current-cycle blocker ledger for architecture component inventory review. It is not long-term review memory.

```yaml
schema_version: 1
mode: "component_inventory"
status: READY_FOR_REVIEW
round: 0
max_rounds: 20
architecture_state: ".codex/agents/architecture-documentation-state.md"
scope_summary: "Review discovered architecture components for correctness and completeness against wired repo paths."
last_actor: "main-agent"
updated_at: "YYYY-MM-DDTHH:MM:SSZ"

fresh_review_policy:
  spawn_new_reviewer_agent_each_cycle: true
  no_prior_review_context_for_reviewer: true
  active_findings_cleared_before_review: true

stop_conditions:
  no_active_blockers: false
  max_rounds_reached: false
  needs_clarification: false

active_findings: []
```

`active_findings` shape:

```yaml
active_findings:
  - id: "INV-R1-P1"
    round: 1
    priority: "P1"
    severity: "blocker"
    category: "missing_component"
    title: "Prompt management is missing from the component inventory."
    status: "open"
    problem: "The inventory omits the shared prompt registry/loader/builder boundary."
    evidence:
      code:
        - "core/prompts/registry.py owns prompt registration."
        - "core/prompts/loader.py loads prompt versions."
      docs:
        - "AGENTS.md lists prompt management as a high-signal entrypoint."
    why_it_blocks: "Architecture docs would miss a shared cross-runtime dependency."
    required_fix: "Add a prompt-management component with primary paths under core/prompts/."
```

