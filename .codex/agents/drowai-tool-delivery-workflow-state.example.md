# drowai-tool-delivery-workflow-state.example.md

Neutral template for the ignored one-tool delivery ledger; copy the YAML block
to `.codex/agents/drowai-tool-delivery-workflow-state.md`, fill the exact
branch/tool/issue values, and never commit the live state.

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
branch_scope:
  diff_range: "origin/main...HEAD"
  changed_files: []
one_tool_only: true
guide: ""
child_states:
  capability: ".codex/agents/drowai-tool-capability-analysis-state.md"
  guide: ".codex/agents/implementation-guide-state.md"
  guide_review: ".codex/agents/implementation-guide-review-state.md"
  implementation: ".codex/agents/implementation-state.md"
  implementation_review: ".codex/agents/implementation-review-state.md"
  mechanical: ".codex/agents/drowai-tool-mechanical-validation-state.md"
  quality: ".codex/agents/implementation-quality-review-state.md"
phase_commits: []
gates:
  capability: pending
  guide_review: pending
  phase_reviews: pending
  final_implementation: pending
  user_guide_current: pending
  mechanical: pending
  quality: pending
  final_tests: pending
quality:
  refactor_suggestions: []
  executed_suggestions: []
  deferred_suggestions: []
documentation:
  user_guide_checked: false
  user_guide_updated: false
  browser_runbook_checked: false
  browser_runbook_updated: false
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

Valid statuses are `READY`, `BRANCH_READY`, `ANALYZING`,
`DECISION_PENDING`, `DECISION_RECORDED`, `GUIDE_REVIEW`, `IMPLEMENTING`,
`IMPLEMENTATION_REVIEW`, `MECHANICAL_VALIDATION`, `QUALITY_REVIEW`,
`REFACTORING`, `FINAL_REVIEW`, `READY_FOR_PR`, `PR_OPENED`, `BLOCKED`, and
`NEEDS_CLARIFICATION`.

`PR_OPENED` is terminal for the run and must be followed by ignored-state
cleanup and an immediate stop.
