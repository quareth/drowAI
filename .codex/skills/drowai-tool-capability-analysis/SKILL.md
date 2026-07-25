---
name: drowai-tool-capability-analysis
description: Analyze one DrowAI pentesting tool through its real registered, Kali-installed, official CLI, and LLM-visible paths before implementation. Use when evaluating a milestone tool, proving its executable exists in the task Kali runtime, reconciling old tool definitions with current official documentation, comparing it with the Amass reference, or identifying the exact foundation work required before an implementation guide is created.
---

# DrowAI Tool Capability Analysis

Evaluate exactly one candidate tool and prepare only obvious CLI-contract
corrections before guide creation. Wired code, the installed Kali executable,
and version-matched official tool documentation are the authorities for their
respective contracts.

## Required inputs

- Exact candidate `tool_id`. If a name maps to multiple registry IDs, stop with
  `NEEDS_CLARIFICATION` until one ID is selected.
- Source issue and milestone context, when available.
- Current tool branch and `origin/main` base; use only that checked-out branch
  for inspection and bounded corrections.
- A workflow-owned disposable task/runtime and authenticated task-control path
  for the Kali installation check.

The durable ledger is
`.codex/agents/drowai-tool-capability-analysis-state.md`. Initialize it from
`.codex/agents/drowai-tool-capability-analysis-state.example.md`. The live
state is ignored and must never be staged.

## Boundaries

- The analyzer agent remains read-only except for the ignored capability state.
- Stay on the recorded tool branch and do not inspect or switch to other
  branches.
- The main agent may create one disposable task/runtime, inspect the installed
  executable, browse primary official tool documentation, and directly correct
  obvious drift in existing selected-tool function or definition files.
- Use the authenticated task API and runtime-provider boundary for the Kali
  check; never call Docker internals directly.
- Do not add modules, redesign the tool, refactor adjacent code, change
  visibility, or modify unrelated files during drift correction.
- Do not create a separate drift agent, guide, or review workflow.
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
2. As the main agent, trace only far enough through the selected registry entry,
   tool class, command builder, and args model to identify the executable,
   documented version command, and existing function/definition files.
3. Prove the executable is installed in a real task Kali runtime:
   - derive one executable token from the selected tool's wired command builder,
     never from issue text or other untrusted input;
   - reject shell metacharacters and quote the token before command execution;
   - create a disposable task through the same task lifecycle used by
     `kalitool`;
   - call the authenticated provider-mediated
     `/api/docker/execute-command/{task_id}` path with
     `command -v -- <executable>`, then the tool's documented version command;
   - record the resolved container path, installed version, and secret-safe
     evidence, then delete the task/runtime.
4. Review upstream CLI drift before guide creation:
   - inspect the installed version's local help or man page;
   - consult only primary official project documentation, versioned manuals,
     release notes, or the upstream source repository;
   - record source URLs, retrieval date, documented version, and any difference
     from the installed Kali version;
   - compare subcommands, flags, required arguments, defaults, enums, output
     modes, and exit behavior with the existing args model, tool function
     schema/description, registry definition, and command builder.
5. Fix only obvious, bounded mismatches directly on the current branch, such as
   a renamed flag, missing required subcommand, invalid default, stale enum, or
   incorrect function-schema description:
   - edit only existing selected-tool function/definition files;
   - prefer the installed version's local contract when latest upstream docs
     describe a different release;
   - run the smallest existing focused checks that cover the corrected schema
     or command builder;
   - commit the correction before guide creation and record its SHA;
   - route ambiguous, architectural, parser, compression, knowledge, or
     multi-component work into the implementation guide instead of expanding
     this correction.
6. Spawn `drowai-tool-capability-analyzer` after any direct correction commit;
   it must trace the exact post-correction ID through the current registry,
   tool class, planner schema, executor/runtime dispatch, visibility policy,
   compression, semantic, and knowledge paths.
7. Require the analyzer and main agent to complete
   [references/capability-checklist.md](references/capability-checklist.md) and
   write evidence paths into the live state.
8. Route from state:
   - `SUITABLE`: create the per-tool implementation guide.
   - `NEEDS_FOUNDATION`: guide the recorded missing mechanics before exposure.
   - `DEFERRED`: record the concrete dependency or prerequisite and stop.
   - `NOT_PLANNED`: prepare a documented decision; do not implement.
   - `NEEDS_CLARIFICATION`: ask only for the unresolved tool/scope decision.
9. Return the decision, installed executable/version, official-documentation
   evidence, corrections, decisive wired evidence, blockers, and next route.

## Required evidence matrix

Assess each dimension separately:

1. Registry identity and concrete `BaseTool` class.
2. Installed Kali executable path and version from a real task runtime.
3. Version-matched official CLI contract and recorded documentation sources.
4. Execution args schema, required fields, defaults, validators, and safe target
   needs.
5. Planner/function schema and parameter preservation.
6. Executor/runtime command construction and runtime-provider dispatch.
7. Output parsing, success/empty/partial/failure semantics, and artifacts.
8. Semantic observations/evidence.
9. Deterministic compression and exact `total` / `shown` / `omitted`
   accounting.
10. Post-tool-reasoning projection.
11. Knowledge adapter and useful engagement facts.
12. Focused tests and canonical docs.
13. LLM catalog visibility and selected-category reachability, checked last.

`registered` and `llm_visible` are never interchangeable.

## Decision rules

- `SUITABLE` requires a proven installed executable, completed official CLI
  review, resolved obvious definition drift, and an implementation-ready
  bounded gap list; it does not require the tool to be visible yet.
- A missing executable cannot advance to guide creation; record `DEFERRED` with
  the Kali image/runtime prerequisite and return to tool selection.
- Failure to identify the installed version or a trustworthy matching contract
  is `NEEDS_CLARIFICATION`, not permission to follow latest docs blindly.
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
