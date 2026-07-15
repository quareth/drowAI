---
program_root: "docs/refactor/<program-slug>"
refactor_type: "structural" # structural | rename_identifier | mixed
guide: "docs/refactor/<program-slug>/README.md"
statement: "docs/refactor/<program-slug>/statement.md"
safety_rules: "docs/refactor/<program-slug>/safety-rules.md"
naming_map: "" # required for rename_identifier; optional for structural
related_designs:
  - "docs/refactor/RULES.md"
  - "docs/refactor/<program-slug>/statement.md"
  - "docs/refactor/<program-slug>/safety-rules.md"
intent_summary: "Short summary of what the refactor program is intended to deliver."
review_scope: "full_program" # full_program | single_phase | section
phase_selector: "" # optional phase filename when review_scope=single_phase
section_selector: "" # optional heading or anchor when review_scope=section
blocker_only: true
hard_cap_rounds: 20
preserve_structure: true
---

# Refactor Guide State Example

Source-of-truth state for **refactor-guide-creator** only. The creator agent reads and updates this file — nothing else.

**Not used by refactor-guide-creator:** `implementation-state.md`, `implementation-review-state.md`, `implementation-guide-state.md`. Set those separately when you are ready to execute an approved guide.

## Field reference

| Field | Purpose |
|-------|---------|
| `program_root` | Directory containing the refactor program docs |
| `refactor_type` | `structural`, `rename_identifier`, or `mixed` |
| `guide` | Primary entry doc (`README.md` or phase guide) |
| `statement` | Problem statement + structural map (structural/mixed) |
| `safety_rules` | Binding execution rules for the program |
| `naming_map` | Old → new map (rename programs) |
| `related_designs` | Supporting docs the creator/reviewer must read |
| `intent_summary` | One-paragraph program goal for agent handoff |
