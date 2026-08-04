---
name: drowai-tool-delivery-workflow
description: Deliver exactly one DrowAI pentesting tool selected from the Phase 01 milestone description on one Codex branch through repository-discovered mature wiring references, real-Kali installation proof, official CLI drift correction, reviewed implementation guide, phase-reviewed commits, safe Kali and GUI mechanics, branch-scoped quality work, push, and a milestone-attached pull request. Use when starting or continuing the milestone’s selected-tool backlog; a per-tool issue is optional and multiple tools must never share one branch or PR.
---

# DrowAI Tool Delivery Workflow

Act as the main-agent router and delegate to the existing specialist skills and
agents without replacing their state contracts.

## Invariants

- Treat the milestone description's `Selected tools` list as the authoritative
  backlog and preserve its order and explicit statuses.
- Use one exact milestone entry, one tool ID, one branch, and one PR per run.
- A linked issue is optional supporting context and never a selection
  prerequisite.
- Create the branch before invoking any workflow agent and keep every action on
  that branch.
- Give every agent the current branch, `origin/main` base, selected milestone
  entry, tool ID, optional issue, and reviewed guide when one exists.
- Agents may inspect repository context, but reviews, fixes, quality work, and
  refactors must remain within `origin/main...HEAD` and the selected tool.
- Stop after the branch is pushed, the PR is created and attached to the
  milestone, and ignored workflow state is cleaned; do not wait for merge.
- Never stage live state, `docs/devdocs/`, reports, browser artifacts,
  credentials, cookies, or private targets.

Use `.codex/agents/drowai-tool-delivery-workflow-state.md` as the ignored
ledger, initialized from its committed example.

## 1. Select the tool and create its branch

1. Read `AGENTS.md`, `CONTRIBUTING.md`, the current milestone description, and
   PRs already attached to that milestone.
2. Parse the numbered `Selected tools` list and classify each entry:
   - an explicit `Completed`, `Deferred`, `Abandoned`, or equivalent final
     annotation is terminal;
   - an attached open PR whose body identifies the milestone tool and exact
     tool ID is `PR_OPEN`;
   - an attached merged PR is `COMPLETED` and may reconcile that exact
     milestone line to `Completed`;
   - a closed-unmerged PR without a final milestone annotation returns the tool
     to `PENDING`;
   - unrelated milestone issues or PRs without a selected-tool marker do not
     represent a tool entry.
3. Select the user-named pending entry or otherwise the first pending entry in
   milestone order, and never select a terminal or `PR_OPEN` entry.
4. Resolve that entry to one exact current registry `tool_id`; stop on an
   ambiguous name or duplicate registration.
5. Require a clean worktree, update local `main` from `origin/main`, and create
   `codex/feat/<tool-slug>-full-wiring`.
6. Record the milestone entry, tool ID, optional issue, `base_ref: origin/main`,
   and current branch in every workflow state.
7. Inspect live state before resetting it and never overwrite another active
   workflow.

## 2. Analyze capability

Run `$drowai-tool-capability-analysis` on the current branch.

- Discover the current registered and LLM-visible tool surfaces from active
  code, then select the closest mature reference separately for schema/runtime,
  results/artifacts, semantics, compression/PTR, knowledge, and visibility.
- Inspect Nmap first for network-scanning responsibilities, but require wired
  evidence and choose a closer mature analogue when appropriate.
- Do not create the guide until a disposable real Kali task proves the selected
  executable is installed and records its path and version.
- Compare the installed version's local CLI contract with primary official
  version-matched documentation and the existing tool function, args model,
  function schema/description, registry definition, and command builder.
- The main agent directly fixes and commits only obvious selected-tool
  definition drift before guide creation; do not create another agent, guide,
  or workflow for this correction.
- Record broader or ambiguous drift as an implementation-guide gap instead of
  turning the preflight into a refactor.
- `SUITABLE` or bounded `NEEDS_FOUNDATION` continues.
- A missing Kali executable records `DEFERRED`, returns to tool selection, and
  does not start implementation.
- `DEFERRED` or `NOT_PLANNED` records a concise evidence-backed status and
  reason on only the selected milestone-list entry, preserves every other
  milestone-description byte, cleans this run's state, and stops without
  implementation.
- `NEEDS_CLARIFICATION` stops for the exact unresolved tool or scope decision.

Use mature tools only as responsibility-specific producer references. Reuse
`runtime_shared/semantic/pentest_facts`, the Knowledge fact bridge, and compact
fact projection as the mandatory semantic architecture. For an existing fact
family, do not create a per-tool Knowledge adapter, compression adapter,
registry entry, compressor import, or pentest `compact_*` override. Do not copy
another tool's timeout, command, parsing, or semantic policy without evidence.

## 3. Create and review the implementation guide

1. Require the Kali-installation and official-contract gates to be passed and
   require the repository reference matrix and any direct drift-correction
   commit to be recorded.
2. Run `implementation-guide-creator` with the branch, milestone entry, tool
   ID, optional issue context, capability evidence, responsibility-specific
   mature references, installed version, official documentation sources, and
   deferred non-obvious drift.
3. Write the ignored guide under `docs/devdocs/plan/`.
4. Run `$implementation-guide-review-loop` until its state is `COMPLETE`.
5. Initialize schema-2 implementation state with the reviewed guide and full
   first task ID.

Do not create an empty commit for an ignored guide.

## 4. Implement and commit each phase

Run `$feature-implementation-workflow` one guide task at a time.

For every phase:

1. Complete all phase tasks.
2. Run `$implementation-review-loop` in `current_phase` mode.
3. Fix blockers and repeat with a fresh reviewer until `COMPLETE`.
4. Run the phase's focused checks.
5. Commit the successful phase before beginning the next phase.

After the final phase, require implementation state `COMPLETE` and run
`$implementation-review-loop` in `final_implementation` mode.

## 5. Validate mechanics and current GUI guidance

Run `$drowai-tool-mechanical-validation` against the committed branch.

Before browser testing:

1. Compare `docs/runbooks/ai-agent-user-guide.md` and
   `docs/runbooks/browser-testing-scenarios.md` with current routes, labels,
   model selection, and a fresh browser snapshot.
2. Update stale guidance on the current branch before using it.
3. Use the current Operations task panel and select **Open models** →
   **GPT-OSS 20B** → **NVIDIA**.

`FAIL` returns to the affected implementation phase and fresh phase/final
review; `INCONCLUSIVE` is reserved for bounded model-selection uncertainty;
`NEEDS_CLEANUP` blocks until workflow-created runtime state is removed.

Commit reviewed mechanical fixes and any verified user-guide correction.

## 6. Run branch-scoped quality and direct refactors

1. Require a clean worktree with all implementation changes committed.
2. Reset quality state with `scope.kind: branch`,
   `scope.target_ref: <current-branch>`, `scope.base_ref: origin/main`, and
   `scope.locked: false`.
3. Run `$implementation-quality-review-loop` until `COMPLETE`.
4. Commit bounded behavior-neutral quality fixes.
5. Read every suggestion created under `docs/devdocs/refactor/` and execute it
   directly without creating a separate refactor guide.
6. Before executing a suggestion, use
   `git diff --name-only origin/main...HEAD` as the refactor allowlist.
7. Modify only files already changed by this branch; defer any suggestion that
   requires untouched files, wider architecture, public contracts, schema
   changes, or unrelated components.
8. Review and commit each accepted refactor, then rerun the original-guide
   final review, affected mechanical checks, and fresh branch quality review.

Refactor suggestion files remain ignored and are never committed.

## 7. Run final contribution checks

Run:

- focused tool/parser/runtime/semantic compiler/compact projection/Knowledge
  bridge and read-model checks;
- `python3 scripts/check_publication_safety.py`;
- `git diff --check`;
- frontend checks only when the branch changed frontend contracts;
- a final implementation review against the original tool guide.

Update only affected canonical documentation and add one concise
`CHANGELOG.md` `[Unreleased]` entry without changing product versions.

## 8. Publish and stop

1. Verify the staged scope contains only intended branch changes.
2. Commit the final documentation and validation changes.
3. Push the branch and create a PR following `CONTRIBUTING.md`, including
   problem, solution, risks, validation, user-facing changes, AI assistance,
   `Milestone tool: <exact-list-name>`, and `Tool ID: <exact-tool-id>`; include
   `Closes #<issue>` only when an optional issue exists.
4. Attach the PR to Milestone #1.
5. Record `PR_OPENED`, preserve the PR URL for the final response, remove only
   this run's ignored live states, and stop immediately.

Do not monitor, wait for, or perform the merge, and do not start another tool
within the same run.

## Recovery

- Never bypass a failed child workflow by editing its status.
- Reinitialize review and quality state before each fresh scope.
- Preserve the branch and commits when an external action fails, then retry
  only that external action.
- Stop for user direction when required work would exceed the selected
  milestone entry or branch diff.

## Validation

```bash
python3 /Users/gunesalcan/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/drowai-tool-delivery-workflow
```
