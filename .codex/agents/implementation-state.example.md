# implementation-state.example.md

Neutral reset template for `.codex/agents/implementation-state.md`. Copy the
YAML block into the local state file, then replace every placeholder with the
approved guide scope. The live state file is intentionally ignored by Git.

```yaml
---
guide: "docs/path/to/implementation-guide.md"
guide_structure: "task_nm"
phase: "0"
task: "0.1"
intent_summary: "Concise statement of the approved implementation outcome."
advance_after_complete: true
ownership_checklist:
  - "scope-boundary — change only the behavior authorized by the guide"
  - "source-of-truth — verify wired code paths before relying on documentation"
  - "separation-of-concerns — preserve established architectural boundaries"
  - "secure-by-design — preserve authorization, isolation, and secret handling"
  - "test-first — reproduce behavior and validate the smallest relevant scope"
  - "documentation — update only canonical documents affected by behavior changes"
  - "module-docstrings — new modules state their purpose and responsibility"
---
```

Use `advance_after_complete: false` when recording final closure. Phase and task
values must match the selected guide; workflows must not infer them from this
example.
