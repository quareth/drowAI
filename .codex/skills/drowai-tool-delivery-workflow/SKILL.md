---
name: drowai-tool-delivery-workflow
description: Deliver exactly one DrowAI milestone pentesting tool on one Codex branch from its attached evaluation issue through real-Kali installation proof, official CLI drift correction, wired-path analysis, reviewed implementation guide, phase-reviewed commits, safe Kali and GUI mechanics, branch-scoped quality work, push, and pull request. Use when starting or continuing one Phase 01 tool contribution; never batch multiple tools into one branch or PR.
---

# DrowAI Tool Delivery Workflow

Act as the main-agent router and delegate to the existing specialist skills and
agents without replacing their state contracts.

## Invariants

- Use one exact tool ID, one evaluation issue, one branch, and one PR per run.
- Create the branch before invoking any workflow agent and keep every action on
  that branch.
- Give every agent the current branch, `origin/main` base, selected issue,
  tool ID, and reviewed guide when one exists.
- Agents may inspect repository context, but reviews, fixes, quality work, and
  refactors must remain within `origin/main...HEAD` and the selected tool.
- Stop after the branch is pushed, the PR is created and attached to the
  milestone, and ignored workflow state is cleaned; do not wait for merge.
- Never stage live state, `docs/devdocs/`, reports, browser artifacts,
  credentials, cookies, or private targets.

Use `.codex/agents/drowai-tool-delivery-workflow-state.md` as the ignored
ledger, initialized from its committed example.

## 1. Select the tool and create its branch

1. Read `AGENTS.md`, `CONTRIBUTING.md`, and the selected milestone issue.
2. Resolve one exact tool ID and stop on ambiguous duplicate registrations.
3. Require a clean worktree, update local `main` from `origin/main`, and create
   `codex/feat/<tool-slug>-full-wiring`.
4. Record `base_ref: origin/main` and the current branch in every workflow
   state.
5. Inspect live state before resetting it and never overwrite another active
   workflow.

## 2. Analyze capability

Run `$drowai-tool-capability-analysis` on the current branch.

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
- Other `DEFERRED` or `NOT_PLANNED` outcomes record the evidence-backed issue
  decision, clean this run's state, and stop without implementation.
- `NEEDS_CLARIFICATION` stops for the exact unresolved tool or scope decision.

Use Amass as the completed reference and reuse
`budget_rendered_items`; do not copy Amass-specific timeout policy.

## 3. Create and review the implementation guide

1. Require the Kali-installation and official-contract gates to be passed and
   any direct drift-correction commit to be recorded.
2. Run `implementation-guide-creator` with the branch, tool ID, issue,
   capability evidence, installed version, official documentation sources, and
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

- focused tool/parser/runtime/semantic/compression/knowledge checks;
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
   and `Closes #<issue>`.
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
- Stop for user direction when required work would exceed the selected issue
  or branch diff.

## Validation

```bash
python3 /Users/gunesalcan/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/drowai-tool-delivery-workflow
```
