# implementation-guide-state.example.md

Template for `.codex/agents/implementation-guide-state.md`.

This file is the source of truth for implementation-guide review scope. It is separate from code-implementation state files.

```yaml
---
guide: "docs/path/to/implementation-guide.md"
related_design: "docs/path/to/high-level-design.md"
intent_summary: "Short summary of what the guide is intended to deliver."
review_scope: "full_guide" # full_guide | section | current_phase
phase: "" # required when review_scope=current_phase
section_selector: "" # optional heading or anchor when review_scope=section
blocker_only: true
hard_cap_rounds: 20
preserve_structure: true
---
```
