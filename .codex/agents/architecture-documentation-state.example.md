# architecture-documentation-state.example.md

Template for `.codex/agents/architecture-documentation-state.md`.

This is the durable source of truth for architecture documentation inventory, per-component progress, and daily drift-audit metadata. Copy the YAML block into `.codex/agents/architecture-documentation-state.md` when starting or resetting the workflow.

```yaml
---
schema_version: 1
status: "DISCOVERY_READY" # DISCOVERY_READY | INVENTORY_REVIEW_READY | COMPONENT_READY | DOC_IN_PROGRESS | DOC_REVIEW_READY | ALL_COMPLETE | NEEDS_CLARIFICATION | MAX_ROUNDS_REACHED
current_component: ""
last_actor: "main-agent"
updated_at: "YYYY-MM-DDTHH:MM:SSZ"

drift_audit:
  enabled: true
  last_run_at: ""
  last_checked_git_ref: ""
  mode: "all_components" # all_components | changed_components
  update_docs_when_drift_found: true

components:
  - id: "backend-control-plane"
    name: "Backend Control Plane"
    status: "pending_doc" # pending_doc | doc_in_progress | doc_review_ready | doc_complete | blocked
    doc_path: "docs/architecture/management-plane.md"
    summary: "FastAPI routing, auth, task lifecycle orchestration, and service handoff boundaries."
    primary_paths:
      - "backend/main.py"
      - "backend/routers/tasks/__init__.py"
    wired_entrypoints:
      - "backend/main.py"
    related_components:
      - "task-runtime-container-lifecycle"
      - "streaming-event-fanout"
    evidence_notes:
      - "AGENTS.md identifies backend/main.py and task router as high-signal wired entrypoints."
    last_doc_updated_at: ""
    last_doc_verified_at: ""
    last_verified_git_ref: ""
    drift_status: "unknown" # unknown | clean | pending_review | drift_found | updated | blocked
---
```

## Status routing

| Status | Meaning |
| --- | --- |
| `DISCOVERY_READY` | Component discovery should run. |
| `INVENTORY_REVIEW_READY` | Fresh inventory reviewer should verify the component list. |
| `COMPONENT_READY` | Main agent should choose the next `pending_doc` component and call the doc writer. |
| `DOC_IN_PROGRESS` | A writer is working on the current component. |
| `DOC_REVIEW_READY` | A fresh doc reviewer should review the current component doc. |
| `ALL_COMPLETE` | All known components have reviewed docs. |
| `NEEDS_CLARIFICATION` | State has concrete missing input or ambiguous scope. |
| `MAX_ROUNDS_REACHED` | Automated review hit the hard cap and needs human decision. |
