---
doc_root: "docs/<program>/"
file_glob: "phase-*.md"
last_completed_index: -1
last_completed_file: ""
guide_mode: "refactor"
pipeline_stage: "idle"
current_index: 0
current_input_doc: ""
current_guide: ""
refactor_type: "rename_identifier"
hard_stop: null
hard_stop_reason: ""
updated_at: ""
---

# Program Workflow State Example

Minimal router state. User sets **`doc_root`** only. Numbered phase files live under that directory.

## User sets

| Field | Example | Meaning |
|-------|---------|---------|
| `doc_root` | `docs/refactor/my-program/` | Directory containing numbered phase docs |
| `file_glob` | `phase-*.md` | Discovery pattern (sorted lexicographically) |
| `last_completed_index` | `1` | Last **fully finished** item (0-based). `-1` = none done. Next run starts at `last_completed_index + 1` |
| `guide_mode` | `refactor` \| `feature` | Which guide creator to trigger |
| `pipeline_stage` | `creating_guide` to start | Resume stage after interruption |

## Router discovers (do not list in state)

1. Glob `doc_root` + `file_glob`, sort.
2. `current_index` = `last_completed_index + 1`.
3. `current_input_doc` = sorted list[`current_index`] — this is the **phase input doc** for guide creator (not README, not implementation guide).

## After each full cycle (create → review → implement → final review)

```text
last_completed_index += 1
last_completed_file ← basename of finished input doc (trace only)
current_guide ← ""
current_input_doc ← ""
pipeline_stage ← creating_guide  (if more files) else program_complete
```

Router must **immediately** start the next phase in the same session when more files exist — never stop after one cycle.

## Optional refactor metadata

If `guide_mode: refactor`, router may copy `doc_root`-relative paths into `refactor-guide-state` when present on disk (`safety-rules.md`, `naming-map.md`, program `README.md` as program context — not as phase input).
