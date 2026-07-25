# drowai-tool-delivery-workflow-state.example.md

Neutral template for the ignored one-tool delivery ledger. Copy the YAML block
to `.codex/agents/drowai-tool-delivery-workflow-state.md`, fill the exact
tool/issue values, and never commit the live state.

```yaml
---
schema_version: 1
status: READY
status_reason: ""
resume_status: ""
tool:
  name: "<tool-name>"
  tool_id: "category.subcategory.tool_name"
  issue_number: 0
  issue_url: ""
milestone:
  number: 1
  title: "Phase 01: Reliable Pentesting Tools — Collection 1"
base_ref: "origin/main"
branch: "codex/feat/<tool-slug>-full-wiring"
prior_delivery:
  outcome: "unknown" # none | merged | decision_recorded | blocked
  reference: ""
one_tool_only: true
next_tool_allowed: false
guide: ""
child_states:
  capability: ".codex/agents/drowai-tool-capability-analysis-state.md"
  guide: ".codex/agents/implementation-guide-state.md"
  guide_review: ".codex/agents/implementation-guide-review-state.md"
  implementation: ".codex/agents/implementation-state.md"
  implementation_review: ".codex/agents/implementation-review-state.md"
  mechanical: ".codex/agents/drowai-tool-mechanical-validation-state.md"
  quality: ".codex/agents/implementation-quality-review-state.md"
  refactor: ".codex/agents/refactor-guide-state.md"
phase_commits: []
reviewed_code_head: ""
gates:
  capability: pending
  guide_review: pending
  phase_reviews: pending
  final_implementation: pending
  static_security:
    status: pending
    code_head: ""
    report: ""
  mechanical:
    status: pending
    code_head: ""
  quality:
    status: pending
    code_head: ""
  final_tests:
    status: pending
    code_head: ""
quality:
  refactor_round: 0
  max_refactor_rounds: 1
  refactor_suggestions: []
external_actions:
  issue_attached: false
  decision_recorded: false
  decision: ""
  pr_attached: false
pr:
  number: 0
  url: ""
  status: ""
stack:
  was_running: false
  started_by_workflow: false
---
```

Valid statuses are `READY`, `ANALYZING`, `DECISION_PENDING`,
`DECISION_RECORDED`, `BRANCH_READY`, `GUIDE_REVIEW`, `IMPLEMENTING`,
`IMPLEMENTATION_REVIEW`, `SECURITY_REVIEW`, `MECHANICAL_VALIDATION`,
`QUALITY_REVIEW`, `REFACTORING`, `FINAL_REVIEW`, `READY_FOR_PR`, `PR_OPENED`,
`AWAITING_MERGE`, `MERGED`, `BLOCKED`, and `NEEDS_CLARIFICATION`.

Set `next_tool_allowed: true` only after a verified merge or a recorded final
deferred/not-planned decision.
