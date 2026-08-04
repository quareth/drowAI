# drowai-tool-mechanical-validation-state.example.md

Neutral reset template for the ignored live mechanical-validation ledger. Copy
the YAML block to `.codex/agents/drowai-tool-mechanical-validation-state.md`
and replace placeholders. Never commit live state or reports.

```yaml
---
schema_version: 1
status: READY
status_reason: ""
tool_id: "category.subcategory.tool_name"
guide: "docs/devdocs/plan/<tool>-implementation-guide.md"
capability_state: ".codex/agents/drowai-tool-capability-analysis-state.md"
base_ref: "origin/main"
branch: "codex/feat/<tool-slug>-full-wiring"
model:
  preset_id: "nvidia_nim_openai_compatible_chat"
  model_id: "openai/gpt-oss-20b"
  connection_status: "existing_verified"
  secret_marker: "<KEY_SET>"
safe_target:
  kind: "reserved_domain"
  value: "drowai.test"
  source: "reserved_local_fixture"
  controlled_resolver: "127.0.0.1"
  target_override_required: true
schema_runs:
  minimal:
    status: pending
    report_ref: ""
  full:
    status: pending
    report_ref: ""
cases:
  success: {required: true, status: pending, evidence_ref: ""}
  empty: {required: true, status: pending, evidence_ref: ""}
  partial_timeout: {required: true, status: pending, evidence_ref: ""}
  failure: {required: true, status: pending, evidence_ref: ""}
semantic_envelope:
  required: true
  status: pending
  schema_version: ""
  capability_family: ""
  observation_count: 0
  evidence_count: 0
  evidence_ref: ""
canonical_facts:
  required: true
  status: pending
  accepted: 0
  duplicates: 0
  rejected: 0
  diagnostics_by_code: {}
  fact_families: []
  evidence_ref: ""
compression:
  primary_present: false
  canonical_secondary_applicable: true
  canonical_secondary_present: false
  facts_total: 0
  facts_selected: 0
  facts_omitted: 0
  evidence_total: 0
  evidence_selected: 0
  evidence_omitted: 0
  lossiness_risk: ""
  ptr_reachable: false
artifacts: {required: false, status: pending, evidence_ref: ""}
knowledge:
  required: false
  status: pending
  observation_count: 0
  projected_models: []
  lineage_verified: false
  archive_scope_verified: false
  independent_from_compact_omissions: false
  evidence_ref: ""
gui:
  selected_tool_id: ""
  parameters_preserved: false
  tool_result_rendered: false
  selection_attempts: 0
  selection_classification: pending
documentation:
  user_guide_checked: false
  user_guide_updated: false
  browser_runbook_checked: false
  browser_runbook_updated: false
stack:
  was_running: false
  started_by_workflow: false
cleanup:
  task_deleted: false
  runtime_removed: false
  stack_stopped_if_owned: false
  status: pending
report_path: "artifacts/tool-mechanical-<tool-slug>.json"
---
```

Valid statuses are `READY`, `IN_PROGRESS`, `PASS`, `FAIL`, `INCONCLUSIVE`,
`NEEDS_CLEANUP`, and `NEEDS_CLARIFICATION`. Model-selection attempts are capped
at two. Missing schema/parameters, unsafe targeting, runtime failure, or
cleanup failure is never inconclusive.
