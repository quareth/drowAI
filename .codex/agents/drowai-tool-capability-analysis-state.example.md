# drowai-tool-capability-analysis-state.example.md

Neutral reset template for the ignored live capability-analysis ledger. Copy
the YAML block to `.codex/agents/drowai-tool-capability-analysis-state.md` and
replace placeholders. Never commit the live state.

```yaml
---
schema_version: 1
status: READY
status_reason: ""
tool_name: "<tool-name>"
candidate_tool_ids:
  - "category.subcategory.tool_name"
tool_id: "category.subcategory.tool_name"
source_issue: "" # optional
milestone: "Phase 01 milestone"
base_ref: "origin/main"
branch: "codex/feat/<tool-slug>-full-wiring"
repository_references:
  status: unknown
  registered_tool_ids: []
  visible_tool_ids: []
  mature_candidates: []
  selected_by_responsibility:
    tool_schema: ""
    command_runtime: ""
    result_contract_artifacts: ""
    semantics: ""
    compression_ptr: ""
    knowledge: ""
    visibility: ""
  rationale: {}
  evidence: []
budget_reference:
  source_tool_id: "information_gathering.dns.amass"
  helper: "agent/graph/compression/deterministic/budget.py::budget_rendered_items"
  reuse_required: true
  status: unknown
analysis:
  registry:
    status: unknown
    evidence: []
  kali_installation:
    status: unknown
    executable: ""
    resolved_path: ""
    installed_version: ""
    version_command: ""
    temporary_task_cleaned: false
    evidence: []
  official_cli_contract:
    status: unknown
    installed_version: ""
    documented_version: ""
    checked_at: ""
    source_urls: []
    mismatches: []
    evidence: []
  preimplementation_corrections:
    status: unknown
    corrected_files: []
    focused_checks: []
    commit_sha: ""
    deferred_drift: []
    evidence: []
  execution_schema:
    status: unknown
    evidence: []
  planner_function_spec:
    status: unknown
    evidence: []
  executor_runtime:
    status: unknown
    evidence: []
  result_semantics_artifacts:
    status: unknown
    evidence: []
  semantic_observations:
    status: unknown
    evidence: []
  deterministic_compression:
    status: unknown
    evidence: []
  ptr_projection:
    status: unknown
    evidence: []
  knowledge:
    status: unknown
    evidence: []
  tests_docs:
    status: unknown
    evidence: []
  llm_visible:
    status: unknown
    evidence: []
blockers: []
decision: ""
decision_rationale: ""
next_route: ""
external_actions: []
---
```

Valid status values are `READY`, `IN_PROGRESS`, `SUITABLE`, `DEFERRED`,
`NOT_PLANNED`, `NEEDS_FOUNDATION`, and `NEEDS_CLARIFICATION`.
`external_actions` records proposed bookkeeping only; this analysis never
executes external writes.
