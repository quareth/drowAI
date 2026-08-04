# drowai-tool-delivery-workflow-state.example.md

Neutral template for the ignored one-tool delivery ledger; copy the YAML block
to `.codex/agents/drowai-tool-delivery-workflow-state.md`, fill the exact
branch/tool/milestone values, and never commit the live state.

```yaml
---
schema_version: 1
status: READY
status_reason: ""
resume_status: ""
tool:
  name: "<tool-name>"
  tool_id: "category.subcategory.tool_name"
  milestone_entry: "3. <tool-name>"
  milestone_position: 3
  source: "milestone_description"
  issue_number: 0 # optional
  issue_url: "" # optional
milestone:
  number: 1
  title: "Phase 01: Reliable Pentesting Tools — Collection 1"
  url: ""
  entry_status: "PENDING"
  selected_tools_snapshot: []
  attached_tool_prs: []
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
preimplementation:
  repository_references:
    status: pending
    selected_by_responsibility: {}
    evidence: []
  canonical_fact_architecture:
    compiler: "runtime_shared/semantic/pentest_facts/compiler.py::compile_facts"
    policy: "runtime_shared/semantic/pentest_facts/policy.py"
    knowledge_bridge: "backend/services/knowledge/pentest_facts/bridge.py"
    compact_projection: "agent/graph/compression/pentest_facts/projection.py"
    supported_pair_status: pending
    requires_new_fact_family: false
  executable: ""
  resolved_path: ""
  installed_version: ""
  official_docs: []
  contract_review_status: pending
  corrected_files: []
  correction_commit: ""
  deferred_drift: []
  temporary_task_cleaned: false
gates:
  milestone_selection: pending
  repository_reference_discovery: pending
  kali_installation: pending
  official_cli_contract: pending
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
  milestone_entry_reconciled: false
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
