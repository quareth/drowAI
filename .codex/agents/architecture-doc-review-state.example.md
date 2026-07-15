# architecture-doc-review-state.example.md

Template for `.codex/agents/architecture-doc-review-state.md`.

This file is a current-cycle blocker ledger for one component architecture doc review or drift audit. It is not long-term review memory.

```yaml
schema_version: 1
mode: "component_doc" # component_doc | drift_audit
status: READY_FOR_REVIEW
round: 0
max_rounds: 20
architecture_state: ".codex/agents/architecture-documentation-state.md"
component_id: ""
doc_path: ""
scope_summary: "Review one component architecture doc against current code."
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
  - id: "DOC-R1-P1"
    round: 1
    priority: "P1"
    severity: "blocker"
    category: "incorrect_flow"
    title: "Streaming flow omits WebSocket fanout path."
    status: "open"
    location:
      document: "docs/architecture/management-plane.md"
      section: "Runtime Flow"
    problem: "The doc describes SSE only, but backend/main.py exposes WebSocket streaming paths as well."
    evidence:
      doc:
        - "Runtime Flow describes only SSE subscribers."
      code:
        - "backend/main.py defines WebSocket multiplexer paths."
        - "backend/services/streaming/in_memory_hub.py owns fanout."
    why_it_blocks: "The architecture doc is incomplete for a primary runtime event channel."
    required_fix: "Update the flow and diagram to include both SSE and WebSocket fanout through the streaming hub."
```
