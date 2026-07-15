---
name: architecture-drift-audit-workflow
description: Run DrowAI's scheduled architecture documentation drift audit through `.codex/agents/architecture-documentation-state.md`. Use when the user asks to verify architecture docs against current code, run daily architecture doc audit, check for documentation drift, update drifted architecture docs, or create/execute an automation that keeps `docs/architecture/*` aligned with code.
---

# Architecture Drift Audit Workflow

Use this skill to audit completed architecture docs against the current repository and update only docs with real architectural drift.

Durable files:
- `.codex/agents/architecture-documentation-state.md` - component inventory and drift metadata.
- `.codex/agents/architecture-doc-review-state.md` - current-cycle drift findings and review ledger.

## Drift Definition

Treat drift as a material architecture mismatch caused by code changes:
- documented component no longer exists, moved, or changed ownership,
- new architectural responsibility is missing,
- flow, dependency, boundary, security rule, persistence path, runtime ownership, or entrypoint changed,
- Mermaid diagram is now misleading,
- doc assigns ownership to a module that now delegates elsewhere.

Do not treat these as drift:
- minor helper/function/class renames inside the same boundary,
- added tests,
- small implementation details with no architectural impact,
- wording or formatting preferences.

## Workflow

1. Read `.codex/agents/architecture-documentation-state.md`.
2. If state is missing or has no completed components, stop with `NEEDS_CLARIFICATION` and explain that the main architecture documentation workflow must run first.
3. For each component with `status: doc_complete` and an existing `doc_path`:
   - set `current_component`,
   - reset `.codex/agents/architecture-doc-review-state.md` with `mode: drift_audit`, clean `active_findings: []`, component id, and doc path,
   - call a fresh `architecture-doc-drift-auditor`.
4. If drift audit state becomes `COMPLETE`, mark the component `drift_status: clean`, update verification metadata, and continue.
5. If drift audit state becomes `REVIEW_BLOCKED`, call `architecture-doc-fixer`.
6. After fixer resets review state to clean `READY_FOR_REVIEW`, call a fresh `architecture-doc-reviewer` to verify the updated doc.
7. If reviewer returns `COMPLETE`, mark the component `drift_status: updated`, `status: doc_complete`, update verification metadata, and continue.
8. Stop on `NEEDS_CLARIFICATION` or `MAX_ROUNDS_REACHED`; otherwise continue until every completed component has been audited.
9. At the end, update `drift_audit.last_run_at` and stop.

## Automation Prompt

Use this prompt for a daily scheduled automation:

```text
Use $architecture-drift-audit-workflow to verify all completed architecture docs against the current repository. Update only docs with real architectural drift. Use state files as authoritative context and spawn fresh auditor/reviewer agents per component.
```

## Fresh Agent Rule

- Spawn a fresh drift auditor per component.
- Spawn a fresh doc reviewer after every fixer run.
- Do not pass prior audit or fixer reports to the next reviewer.
- Keep only neutral scope metadata and clean `active_findings: []` before each fresh reviewer.

## Hard Rules

- Do not modify application code.
- Do not update docs for non-architectural churn.
- Do not broaden beyond `docs/architecture/*` and architecture state files.
- Do not preserve previous findings visible to reviewers.
- Do not claim daily audit completion without recording state updates.

## Final Response

When the audit stops, report:
- audit status,
- components checked,
- docs updated,
- clean components,
- blockers or clarification needs if present.
