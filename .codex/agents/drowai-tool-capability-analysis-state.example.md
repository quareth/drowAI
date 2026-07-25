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
source_issue: "#NN"
milestone: "Phase 01 milestone"
base_ref: "origin/main"
reference_tool_id: "information_gathering.dns.amass"
analysis:
  registry:
    status: unknown
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
