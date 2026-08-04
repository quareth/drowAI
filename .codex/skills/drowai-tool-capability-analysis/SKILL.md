---
name: drowai-tool-capability-analysis
description: Analyze one DrowAI pentesting tool through repository-discovered mature references and its real registered, Kali-installed, official CLI, and LLM-visible paths before implementation. Use when evaluating a milestone tool, choosing proven in-repo wiring patterns such as Nmap for network-scanning responsibilities, proving the executable exists in the task Kali runtime, reconciling old definitions with current official documentation, or identifying the exact foundation work required before an implementation guide is created.
---

# DrowAI Tool Capability Analysis

Evaluate exactly one candidate tool and prepare only obvious CLI-contract
corrections before guide creation. Mature wired repository paths define DrowAI
integration patterns, while the installed Kali executable and version-matched
official tool documentation define the candidate's CLI contract.

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
- Discover mature active reference tools from current code instead of using a
  fixed universal reference; confirm their registry, production callers, and
  LLM visibility before relying on them.
- Select references per responsibility and use the closest proven analogue;
  Nmap is the first candidate for network-scanning responsibilities, not a
  universal template.
- Treat `runtime_shared/semantic/pentest_facts`, the Knowledge fact bridge, and
  canonical compact fact projection as the mandatory reusable semantic
  architecture. Never create per-tool Knowledge or compression adapters for an
  existing fact family.
- Use mature tools only as producer-side schema, runtime, parser, artifact, and
  semantic-emitter references. Never copy a tool's timeout, inactivity,
  command, parsing, or semantic policy without responsibility-specific
  evidence.
- Visibility is the final enablement step, after the underlying mechanics have
  evidence.

## Workflow

1. Reset the live state from its committed example and record the exact tool ID.
2. Discover the current reference surface from wired code:
   - enumerate registered tools through `available_tools()` and LLM-visible
     tools through `visible_available_tools()`;
   - confirm imports, registry metadata, production call sites, and active
     visibility instead of trusting filenames or stale documentation;
   - identify mature analogues in the candidate's family and adjacent families.
3. Record a responsibility-specific reference matrix for tool/schema,
   command/runtime, result contract/artifacts, semantics, compression/PTR,
   knowledge, and visibility, with evidence and selection rationale for each;
   use Nmap as the first network-scanning candidate and replace it where another
   verified mature tool is a closer match.
4. Map the candidate's intended semantic observations to the exact supported
   pairs in `runtime_shared/semantic/pentest_facts/policy.py`:
   - for an existing fact family, record that Knowledge and compact projection
     reuse requires no tool-specific downstream adapter;
   - for a genuinely new fact family, record the shared policy/compiler,
     Knowledge bridge/projection, compact presentation/projection, and parity
     work as an implementation-guide gap.
5. As the main agent, trace only far enough through the selected registry entry,
   tool class, command builder, and args model to identify the executable,
   documented version command, and existing function/definition files.
6. Prove the executable is installed in a real task Kali runtime:
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
7. Review upstream CLI drift before guide creation:
   - inspect the installed version's local help or man page;
   - consult only primary official project documentation, versioned manuals,
     release notes, or the upstream source repository;
   - record source URLs, retrieval date, documented version, and any difference
     from the installed Kali version;
   - compare subcommands, flags, required arguments, defaults, enums, output
     modes, and exit behavior with the existing args model, tool function
     schema/description, registry definition, and command builder.
8. Fix only obvious, bounded mismatches directly on the current branch, such as
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
9. Spawn `drowai-tool-capability-analyzer` after any direct correction commit;
   it must trace the exact post-correction ID through the current registry,
   tool class, planner schema, executor/runtime dispatch, visibility policy,
   compression, semantic, and knowledge paths and verify every selected
   reference through its own active wired path.
10. Require the analyzer and main agent to complete
   [references/capability-checklist.md](references/capability-checklist.md) and
   write evidence paths into the live state.
11. Route from state:
   - `SUITABLE`: create the per-tool implementation guide.
   - `NEEDS_FOUNDATION`: guide the recorded missing mechanics before exposure.
   - `DEFERRED`: record the concrete dependency or prerequisite and stop.
   - `NOT_PLANNED`: prepare a documented decision; do not implement.
   - `NEEDS_CLARIFICATION`: ask only for the unresolved tool/scope decision.
12. Return the decision, responsibility-specific reference matrix, installed
   executable/version, official-documentation evidence, corrections, decisive
   wired evidence, blockers, and next route.

## Required evidence matrix

Assess each dimension separately:

1. Registry identity and concrete `BaseTool` class.
2. Current registered/visible repository inventory and mature reference matrix.
3. Installed Kali executable path and version from a real task runtime.
4. Version-matched official CLI contract and recorded documentation sources.
5. Execution args schema, required fields, defaults, validators, and safe target
   needs.
6. Planner/function schema and parameter preservation.
7. Executor/runtime command construction and runtime-provider dispatch.
8. Output parsing, success/empty/partial/failure semantics, and artifacts.
9. Semantic observations/evidence.
10. Shared canonical fact admission and deterministic compact projection,
    including selected/omitted counts and lossiness.
11. Post-tool-reasoning projection.
12. Knowledge ingestion/bridge projection and useful scoped engagement facts.
13. Focused tests and canonical docs.
14. LLM catalog visibility and selected-category reachability, checked last.

`registered` and `llm_visible` are never interchangeable.

## Decision rules

- `SUITABLE` requires a repository-discovered reference matrix, proven
  installed executable, completed official CLI review, resolved obvious
  definition drift, and an implementation-ready bounded gap list; it does not
  require the candidate tool to be visible yet.
- A fixed reference chosen without current wired-path evidence blocks
  `SUITABLE`.
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
