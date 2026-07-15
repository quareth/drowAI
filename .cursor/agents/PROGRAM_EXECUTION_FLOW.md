# Program execution flow (router only)

Skill: `.cursor/skills/program-execution-workflow/SKILL.md`.

## One invocation = all remaining phases

The router runs an **outer loop** until `program_complete` or `hard_stop`. It must **not** stop after one phase.

```text
WHILE not program_complete AND not hard_stop:
  creating_guide → reviewing_guide → implementing → reviewing_implementation → advance_queue
  IF more files under doc_root: continue immediately (no user prompt)
END
```

## User input

```yaml
doc_root: "docs/<program>/"
file_glob: "phase-*.md"
last_completed_index: 1
```

## Discovery

```text
sort(glob(doc_root + file_glob))[last_completed_index + 1]  →  current_input_doc
```

## Stop conditions (only these)

| Condition | Action |
|-----------|--------|
| `pipeline_stage: program_complete` | Return final response |
| `hard_stop` set | Return final response |
| `pipeline_stage: idle` | Stop — user must configure |

## Not a stop condition

- One phase cycle finished
- Child skill printed its own "final response"
- `advance_queue` with more files remaining

Handoffs: `.cursor/skills/program-execution-workflow/state-handoffs.md`.
