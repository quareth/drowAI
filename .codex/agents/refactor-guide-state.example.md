# refactor-guide-state.example.md

Neutral reset template for `.codex/agents/refactor-guide-state.md`. Copy the
YAML block into the local state file and replace placeholders for the selected
refactor program. The live state file is intentionally ignored by Git.

```yaml
---
program_root: "docs/devdocs/refactor/<program-slug>"
refactor_type: "structural" # structural | rename_identifier | mixed
guide: "docs/devdocs/refactor/<program-slug>/phase-1-<slug>.md"
statement: "docs/devdocs/refactor/<program-slug>/statement.md"
safety_rules: "docs/devdocs/refactor/<program-slug>/safety-rules.md"
naming_map: "" # required for rename_identifier; optional for structural
related_designs:
  - "docs/runbooks/refactor-runbook.md"
  - "docs/devdocs/refactor/<program-slug>/statement.md"
intent_summary: "Short summary of what the refactor program is intended to deliver."
review_scope: "single_phase" # full_program | single_phase | section
phase_selector: "phase-1-<slug>.md" # required when review_scope=single_phase
section_selector: "" # optional heading or anchor when review_scope=section
blocker_only: true
hard_cap_rounds: 20
preserve_structure: true
---
```

This state belongs only to `refactor-guide-creator`. The program router may
seed it before creator invocation, but implementation and review agents use
their own state files.
