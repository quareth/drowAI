---
name: drowai-tool-capability-analysis
description: Analyze one DrowAI pentesting tool through its real registered and LLM-visible paths before implementation. Use when evaluating a milestone tool, deciding whether it is suitable for full wiring, comparing it with the Amass reference, or identifying the exact foundation work required before an implementation guide is created.
---

# DrowAI Tool Capability Analysis

Evaluate exactly one candidate tool without changing implementation code or
external state. Code and wired call sites are authoritative; documentation is
supporting evidence only.

## Required inputs

- Exact candidate `tool_id`. If a name maps to multiple registry IDs, stop with
  `NEEDS_CLARIFICATION` until one ID is selected.
- Source issue and milestone context, when available.
- Current tool branch and `origin/main` base; use only that checked-out branch
  as read-only evidence.

The durable ledger is
`.codex/agents/drowai-tool-capability-analysis-state.md`. Initialize it from
`.codex/agents/drowai-tool-capability-analysis-state.example.md`. The live
state is ignored and must never be staged.

## Hard boundaries

- Read repository code and run read-only metadata/test commands only.
- Stay on the recorded tool branch and do not inspect or switch to other
  branches.
- The analyzer may write only the ignored capability-analysis state.
- Do not edit tools, tests, guides, prompts, visibility, or GitHub issues.
- Do not start containers, the DrowAI stack, or browser sessions.
- Never infer suitability from a module existing on disk. Confirm a wired path.
- Use Amass as a reference, not a template. In particular, never copy its
  inactivity or wall-clock timeout policy without candidate-specific evidence.
- Reuse `budget_rendered_items` from
  `agent/graph/compression/deterministic/budget.py`; do not duplicate its
  accounting.
- Visibility is the final enablement step, after the underlying mechanics have
  evidence.

## Workflow

1. Reset the live state from its committed example and record the exact tool ID.
2. Spawn `drowai-tool-capability-analyzer`. It must trace the exact ID through
   the current registry, tool class, planner schema, executor/runtime dispatch,
   visibility policy, compression, semantic, and knowledge paths. Use `rg` and
   read-only repository commands; do not introduce a collector or validator.
3. Require the analyzer to follow
   [references/capability-checklist.md](references/capability-checklist.md) and
   write evidence paths into the live state.
4. Route from state:
   - `SUITABLE`: create the per-tool implementation guide.
   - `NEEDS_FOUNDATION`: guide the recorded missing mechanics before exposure.
   - `DEFERRED`: record the concrete dependency or prerequisite and stop.
   - `NOT_PLANNED`: prepare a documented decision; do not implement.
   - `NEEDS_CLARIFICATION`: ask only for the unresolved tool/scope decision.
5. Return the decision, decisive wired evidence, blockers, and next route.

## Required evidence matrix

Assess each dimension separately:

1. Registry identity and concrete `BaseTool` class.
2. Execution args schema, required fields, defaults, validators, and safe target
   needs.
3. Planner/function schema and parameter preservation.
4. Executor/runtime command construction and runtime-provider dispatch.
5. Output parsing, success/empty/partial/failure semantics, and artifacts.
6. Semantic observations/evidence.
7. Deterministic compression and exact `total` / `shown` / `omitted`
   accounting.
8. Post-tool-reasoning projection.
9. Knowledge adapter and useful engagement facts.
10. Focused tests and canonical docs.
11. LLM catalog visibility and selected-category reachability, checked last.

`registered` and `llm_visible` are never interchangeable.

## Decision rules

- `SUITABLE` requires an executable path and an implementation-ready, bounded
  gap list. It does not require the tool to be visible yet.
- Missing schema, required parameter loss, absent executor command, unsafe
  target requirements, or no runtime dispatch path cannot be marked suitable.
- `NEEDS_FOUNDATION` is for a viable tool with identified mechanics to build.
- `DEFERRED` requires a named external or sequencing dependency.
- `NOT_PLANNED` requires evidence that safe, dependable non-interactive support
  is unsuitable, duplicative, unmaintained, or cannot meet the milestone bar.
- Never use prose or prompt quality as a capability criterion.

## Validation

```bash
python3 /Users/gunesalcan/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/drowai-tool-capability-analysis
```
