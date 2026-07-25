# Program Workflow — State Handoffs (router writes only)

Router copies fields between states. Does not edit docs.

All state paths are under `.codex/agents/`.

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

## §1 `creating_guide` → `reviewing_guide`

Seed and trigger the creator selected by `guide_mode`.

For `guide_mode: refactor`:

```text
reset refactor-guide-state from refactor-guide-state.example.md
refactor program_root ← guide_output_root
refactor refactor_type ← program refactor_type
refactor guide ← ""
refactor statement ← current_input_doc
refactor related_designs ← [docs/runbooks/refactor-runbook.md, current_input_doc]
refactor intent_summary ← "Create an executable refactor guide from " + current_input_doc
refactor review_scope ← single_phase
refactor phase_selector ← basename(current_input_doc)
```

Preserve an existing `safety_rules` or `naming_map` only when the configured
path exists and belongs to the current program. Trigger
`refactor-guide-creator`, then require its state `guide` to be a non-empty,
existing path under `docs/devdocs/refactor/`.

For `guide_mode: feature`, trigger `implementation-guide-creator` with
`current_input_doc` and `guide_output_root`. Require the creator to save and
return one existing guide path under `guide_output_root`, which defaults to
`docs/devdocs/plan/`.

After either creator succeeds:

```text
current_guide ← creator output guide path

reset implementation-guide-state from its example
guide-state guide ← current_guide
guide-state related_design ← current_input_doc
guide-state intent_summary ← creator/refactor intent summary
guide-state review_scope ← full_guide
guide-state phase ← ""
guide-state section_selector ← ""

reset implementation-guide-review-state from its example clean state
guide-review guide ← current_guide
guide-review related_design ← current_input_doc
guide-review intent_summary ← guide-state intent_summary
guide-review mode ← full_guide
guide-review phase ← ""
guide-review status ← READY_FOR_REVIEW
guide-review round ← 0
guide-review active_findings ← []

pipeline_stage ← reviewing_guide
```

If the creator does not persist a resolvable guide path, set
`hard_stop: NEEDS_CLARIFICATION`; do not infer an output file.
Also hard-stop if `current_guide` matches the program's discovery input set;
generated guides must not become new `file_glob` inputs during the outer loop.

---

## §2 `reviewing_guide` → `implementing`

Run `implementation-guide-review-loop` until its state reaches a terminal
status. On `COMPLETE`, reset implementation-state from
`implementation-state.example.md`, then populate:

```text
implementation status ← READY
implementation status_reason ← ""
implementation guide ← current_guide
implementation related_design ← current_input_doc
implementation guide_structure ← structure verified from current_guide
implementation phase ← first incomplete phase identifier
implementation task ← first incomplete full task/slice identifier
implementation completed_task ← ""
implementation next_task ← ""
implementation next_phase ← ""
implementation phase_complete ← false
implementation guide_complete ← false
implementation intent_summary ← guide-state intent_summary
implementation advance_after_complete ← true

pipeline_stage ← implementing
```

Use `task_nm` only when the guide has `Task N.M` headings; use `phase_whole` or
`section_batch` only when that structure is explicit. If the starting scope is
ambiguous, set `hard_stop: NEEDS_CLARIFICATION`; do not guess identifiers.

Propagate guide-review `NEEDS_CLARIFICATION` or `MAX_ROUNDS_REACHED` through
the program hard-stop fields without changing the completed index.

---

## §3 `implementing` → `reviewing_implementation`

Run `feature-implementation-workflow` until implementation-state reaches
`COMPLETE` or a hard stop. Successful completion must have:

```text
implementation guide = current_guide
implementation guide_complete = true
implementation next_task = ""
implementation advance_after_complete = false
```

Then reset implementation-review-state from its clean example:

```text
review mode ← final_implementation
review status ← READY_FOR_REVIEW
review round ← 0
review implementation_state ← .codex/agents/implementation-state.md
review guide ← current_guide
review related_design ← implementation related_design
review phase ← ""
review task ← ""
review scope_summary ← "Full implementation review against the current guide."
review intent_summary ← implementation intent_summary
review active_findings ← []

pipeline_stage ← reviewing_implementation
```

Propagate implementation or phase-review `NEEDS_CLARIFICATION` and
`MAX_ROUNDS_REACHED` through the program hard-stop fields.

---

## §4 `reviewing_implementation` → `reviewing_quality`

Run `implementation-review-loop` in `final_implementation` mode until its state
reaches a terminal status. On `COMPLETE`, continue immediately into §5. On
`NEEDS_CLARIFICATION` or `MAX_ROUNDS_REACHED`, set the program hard-stop fields
and leave `last_completed_index` unchanged.

---

## §5 final implementation review `COMPLETE` → quality review

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
