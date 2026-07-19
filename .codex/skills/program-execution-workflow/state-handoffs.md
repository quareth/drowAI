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

## §5 final implementation review → quality review

When final `implementation-review-loop` reaches `COMPLETE`:

```text
reset implementation-quality-review-state from its example clean state
pipeline_stage ← reviewing_quality
quality guide ← current_guide
quality implementation_state ← .codex/agents/implementation-state.md
quality round ← 0
quality scope.kind ← branch
quality scope.target_ref ← current checked-out branch
quality scope.base_ref ← origin/main
quality scope.locked ← false
quality status ← READY_FOR_REVIEW
quality active_findings ← []
quality refactor_suggestions ← []
```

Run `implementation-quality-review-loop` to `COMPLETE`. Its refactor suggestions
are non-blocking. Then:

```text
pipeline_stage ← advance_queue
```

---

## §6 `advance_queue` → next phase or `program_complete`

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

This file is authoritative for Codex program-workflow handoffs.
