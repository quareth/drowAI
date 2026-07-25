---
doc_root: "docs/<program-input>/"
file_glob: "phase-*.md"
guide_output_root: "docs/devdocs/refactor/<program-slug>/"
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

Set `doc_root`, `guide_output_root`, and `last_completed_index`. Router
discovers numbered input files; generated guides must stay outside that
discovery set, so no per-file list belongs in state.

Valid active stages are `creating_guide`, `reviewing_guide`, `implementing`,
`reviewing_implementation`, `reviewing_quality`, and `advance_queue`. The
quality stage completes automatically before the router advances the queue.
