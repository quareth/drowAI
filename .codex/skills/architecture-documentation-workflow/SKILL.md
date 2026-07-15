---
name: architecture-documentation-workflow
description: Run DrowAI's repo-local state-driven architecture documentation workflow through `.codex/agents/architecture-documentation-state.md`. Use when the user asks to discover repo architecture components, create or continue architecture docs, document components, review generated architecture docs, run architecture documentation workflow, or continue architecture documentation from state.
---

# Architecture Documentation Workflow

Use this skill to discover DrowAI architectural components, create one reviewed architecture document per component, and advance through the component inventory with fresh agents.

Durable files:
- `.codex/agents/architecture-documentation-state.md` - component inventory, current component, doc progress, and drift metadata.
- `.codex/agents/architecture-inventory-review-state.md` - current-cycle inventory review ledger.
- `.codex/agents/architecture-doc-review-state.md` - current-cycle component doc review ledger.

## Workflow

1. Read `.codex/agents/architecture-documentation-state.md`.
2. If missing, initialize from `.codex/agents/architecture-documentation-state.example.md`.
3. Route by state:
   - `DISCOVERY_READY`: call `architecture-component-discoverer`.
   - `INVENTORY_REVIEW_READY`: reset `.codex/agents/architecture-inventory-review-state.md` to clean `READY_FOR_REVIEW`, then call a fresh `architecture-component-inventory-reviewer`.
   - `COMPONENT_READY`: choose the first component with `status: pending_doc`, set `current_component`, then call `architecture-doc-writer`.
   - `DOC_REVIEW_READY`: reset `.codex/agents/architecture-doc-review-state.md` for `mode: component_doc`, then call a fresh `architecture-doc-reviewer`.
4. If inventory review state becomes `COMPLETE`, continue to `COMPONENT_READY`.
5. If doc review state becomes `REVIEW_BLOCKED`, call `architecture-doc-fixer`.
6. After fixer resets doc review state to clean `READY_FOR_REVIEW`, call a fresh `architecture-doc-reviewer`.
7. When doc review state becomes `COMPLETE`, mark the current component `status: doc_complete`, clear `current_component`, set state `COMPONENT_READY`, and continue to the next `pending_doc` component.
8. If no pending components remain, set state `ALL_COMPLETE` and stop.
9. Stop only on `ALL_COMPLETE`, `NEEDS_CLARIFICATION`, or `MAX_ROUNDS_REACHED`.

## Fresh Agent Rule

- Spawn a fresh inventory reviewer for each inventory review pass.
- Spawn a fresh doc reviewer for each component doc review pass.
- Do not paste prior reviewer or fixer reports between agents.
- State files are authoritative.
- Before a reviewer runs, ensure `active_findings: []` and no archived findings or fix attempts are present.
- The fixer may read active findings, but must clear them before the next reviewer.

## Component Doc Scope

Each `architecture-doc-writer` run writes exactly one component doc. Docs should be high-level architecture documentation, not implementation guides.

Expected sections:
- Purpose
- Responsibility Boundary
- Wired Entrypoints
- Main Collaborators
- State / Data Flow
- Runtime Flow
- Security / Isolation Notes
- Operational Notes
- Known Gaps Or Drift

Use Mermaid diagrams when they clarify component flow or boundaries.

## Hard Rules

- Code is the source of truth; verify claims through wired paths from `AGENTS.md`.
- Do not modify application code in this workflow.
- Do not let reviewers edit docs.
- Do not let fixers broaden beyond active findings.
- Keep architecture docs under `docs/architecture/`.
- Keep one writer run scoped to one component only.

## Final Response

When the workflow stops, report:
- final architecture documentation state status,
- current component if any,
- completed docs,
- blocked findings or clarification needs if present,
- whether all known components are documented.
