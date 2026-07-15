# Program Workflow — State Handoffs (router writes only)

Router copies fields between states. Does not edit docs.

Paths: `.codex/agents/` (Cursor: `.cursor/agents/`).

---

## Discovery (start of each phase)

```text
sorted_files = sort(glob(doc_root + file_glob))
current_index = last_completed_index + 1
IF current_index >= len(sorted_files):
  pipeline_stage = program_complete
  EXIT outer loop
current_input_doc = sorted_files[current_index]
```

Write `current_index` and `current_input_doc` to `program-workflow-state`.

---

## §5 `advance_queue` → next phase or `program_complete`

```text
last_completed_index += 1
last_completed_file ← basename(current_input_doc)
current_guide ← ""
current_input_doc ← ""
```

Rediscover `sorted_files`.

**If more files remain** (`last_completed_index + 1 < len(sorted_files)`):

```text
pipeline_stage ← creating_guide
→ IMMEDIATELY run §1 discovery + creating_guide (same router session)
```

**If no files remain:**

```text
pipeline_stage ← program_complete
→ EXIT outer loop; now allowed to return final response
```

---

## Hard stop

Child `NEEDS_CLARIFICATION` or `MAX_ROUNDS_REACHED` → set `hard_stop`; **EXIT outer loop**. Do not increment index.

Full §1–§4: see `.cursor/skills/program-execution-workflow/state-handoffs.md`.
