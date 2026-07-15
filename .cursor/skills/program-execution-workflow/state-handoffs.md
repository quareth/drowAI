# Program Workflow — State Handoffs (router writes only)

Router copies fields between states. Does not edit docs.

Paths: `.cursor/agents/` (Codex: `.codex/agents/`).

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

## Field copy rules

| When | Source | Target | Fields |
|------|--------|--------|--------|
| Before creator | `current_input_doc` | `refactor-guide-state` | `statement` ← input doc; `guide: ""` |
| After creator | `refactor-guide-state.guide` | `program-workflow-state` | `current_guide` |
| After creator | input + creator output | `implementation-guide-state` | `related_design` ← input; `guide` ← output |
| After creator | same | `implementation-guide-review-state` | same; `READY_FOR_REVIEW` |
| After guide review `COMPLETE` | impl-guide-state + input | `implementation-state` | `guide`, `related_design` ← input |
| After implementation done | `implementation-state` | `implementation-review-state` | final mode seed |
| After final review `COMPLETE` | router | §5 advance | |

---

## §1 `creating_guide` → `reviewing_guide`

Discover → seed creator with `current_input_doc` → trigger creator → read output → seed guide-review states → `pipeline_stage: reviewing_guide`.

---

## §2 `reviewing_guide` → `implementing`

Trigger `implementation-guide-review-loop` to **completion**. On `COMPLETE` → seed `implementation-state` → `pipeline_stage: implementing`.

---

## §3 `implementing` → `reviewing_implementation`

Trigger `feature-implementation-workflow` to **completion** for current impl guide. On done → seed final review state → `pipeline_stage: reviewing_implementation`.

---

## §4 `reviewing_implementation` → `advance_queue`

Trigger `implementation-review-loop` (final) to **completion**. On `COMPLETE` → run §5 **without stopping the session**.

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
