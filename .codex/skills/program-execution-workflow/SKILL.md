---
name: program-execution-workflow
description: Pure handoff router for DrowAI programs from `.codex/agents/program-workflow-state.md`. Discovers all numbered phase files under doc_root and runs the full create→review→implement→final-review cycle for every remaining item in one session until program_complete or hard_stop. Does not stop after one phase. Does not create, review, or implement itself.
---

# Program Execution Workflow

**Router only.** Runs the **full multi-phase program** in one invocation. Copies state, discovers files, triggers child flows.

## Outer loop contract (strict)

**One user trigger = process every remaining phase file until done.**

```text
WHILE pipeline_stage != program_complete AND hard_stop is null:
  run current stage (handoff → trigger child → read output → update pipeline_stage)
  IF stage was advance_queue AND more files remain:
    IMMEDIATELY continue — do NOT return to user
END
ONLY THEN return final response
```

| MUST | MUST NOT |
|------|----------|
| Continue after `advance_queue` when more files exist | Stop after one phase cycle |
| Re-enter skill loop in the **same session** after each child flow | Ask user to confirm next phase |
| Set `pipeline_stage: creating_guide` and discover next file automatically | Exit with "phase N done" while files remain |
| Stop only on `program_complete`, `hard_stop`, or `idle` | Treat child skill "final response" as router stop |

**Proceed automatically.** At every handoff, trigger the next child flow immediately. Child flows run to completion for the current phase; control always returns to this router until the program finishes.

## User configures

| Field | Purpose |
|-------|---------|
| `doc_root` | Directory with numbered phase docs |
| `file_glob` | Discovery pattern (default `phase-*.md`) |
| `last_completed_index` | Last fully finished item (0-based). `-1` = none done |
| `guide_mode` | `refactor` or `feature` creator |
| `pipeline_stage` | Resume point |

## Discovery (each phase)

```text
sorted = sort(glob(doc_root + file_glob))
current_index = last_completed_index + 1
IF current_index >= len(sorted) → pipeline_stage = program_complete; STOP loop
current_input_doc = sorted[current_index]
```

Persist `current_index` and `current_input_doc` in `program-workflow-state` when starting a phase.

## Stage machine (one phase)

| `pipeline_stage` | Trigger | On success → |
|------------------|---------|--------------|
| `creating_guide` | guide creator | `reviewing_guide` |
| `reviewing_guide` | `implementation-guide-review-loop` | `implementing` |
| `implementing` | `feature-implementation-workflow` | `reviewing_implementation` |
| `reviewing_implementation` | `implementation-review-loop` (final) | `advance_queue` |
| `advance_queue` | *(router only)* | `creating_guide` or `program_complete` |

Handoffs: [state-handoffs.md](state-handoffs.md).

## After `advance_queue` (critical)

1. `last_completed_index += 1`
2. Rediscover `sorted` from `doc_root`
3. If `last_completed_index + 1 < len(sorted)`:
   - Clear `current_guide`, `current_input_doc`
   - Set `pipeline_stage: creating_guide`
   - **Immediately run `creating_guide` for the next file — same session, no user prompt**
4. Else: `pipeline_stage: program_complete` → exit loop

## Hard stops (only valid stop)

Set `hard_stop` from child state on `NEEDS_CLARIFICATION` or `MAX_ROUNDS_REACHED`. Do not advance `last_completed_index`. Stop loop and report.

## Hard rules

- Never stop after one phase if more discovered files remain.
- Never use README as phase input unless it is the file at `current_index`.
- Never list phase paths in state — discover from `doc_root`.
- Never create, review, or implement in this skill.

## Final response (only when loop exits)

Report: `program_complete` or `hard_stop`, `last_completed_index`, phases processed this run, `doc_root`.
