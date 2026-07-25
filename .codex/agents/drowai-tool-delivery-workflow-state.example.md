# drowai-tool-delivery-workflow-state.example.md

Neutral reset template for the ignored one-tool delivery ledger. Copy the YAML
block to `.codex/agents/drowai-tool-delivery-workflow-state.md`, replace
placeholders, and never commit the live state.

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
  title: "Phase 01 milestone"
base_ref: "origin/main"
branch: "codex/feat/<tool-slug>-full-wiring"
one_tool_only: true
next_tool_allowed: false
capability_state: ".codex/agents/drowai-tool-capability-analysis-state.md"
mechanical_state: ".codex/agents/drowai-tool-mechanical-validation-state.md"
implementation_state: ".codex/agents/implementation-state.md"
guide: ""
phase_commits: []
validation_gates:
  capability: pending
  guide_review: pending
  phase_reviews: pending
  final_implementation: pending
  static_security: pending
  mechanical_validation: pending
  quality_review: pending
  final_tests: pending
static_security_review:
  route: "static-security-analyzer"
  report_ref: ""
  conclusion: ""
  blocking_findings: []
quality:
  target_ref: ""
  base_ref: "origin/main"
  refactor_round: 0
  max_refactor_rounds: 1
  refactor_suggestions: []
external_actions:
  issue_attached: false
  decision_recorded: false
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

`next_tool_allowed` becomes true only for verified `MERGED` or
`DECISION_RECORDED` outcomes.
