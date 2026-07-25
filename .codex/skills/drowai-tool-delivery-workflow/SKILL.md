---
name: drowai-tool-delivery-workflow
description: Deliver exactly one DrowAI milestone pentesting tool from an attached evaluation issue through wired-path analysis, reviewed implementation guide, phased implementation/reviews/commits, safe Kali and GUI mechanics, security and quality gates, push, and pull request. Use when starting or continuing one Phase 01 tool contribution; never use it to batch multiple tools into one branch or PR.
---

# DrowAI Tool Delivery Workflow

This is a main-agent router. It delegates to existing skills and agents; it
does not replace their reviewer/fixer contracts.

## One-tool invariant

- One exact tool ID, one evaluation issue, one short-lived branch, one PR.
- Do not start a second tool while an earlier tool-delivery PR is open.
- After a PR is created, stop at `AWAITING_MERGE`. A later invocation must
  verify merge before selecting the next issue.
- A rejected/deferred tool may advance only after its evaluation issue records
  the decision and is closed or explicitly left deferred.

The durable local ledger is
`.codex/agents/drowai-tool-delivery-workflow-state.md`, initialized from the
committed example. It is ignored and never committed.

## Binding rules

1. Read `AGENTS.md`, `CONTRIBUTING.md`, and current wired code before acting.
2. Start from updated `main`; use a `codex/feat/<tool-slug>-full-wiring` branch
   in Codex.
3. Use the issue already attached to Milestone #1. Create/attach one before
   implementation only when the user authorized that GitHub write.
4. Live state, `docs/devdocs/`, `artifacts/`, browser output, credentials, and
   private targets are never staged.
5. Runtime effects remain inside task/runtime-provider boundaries.
6. Commit coherent reviewed phases. Quality review sees committed Git scope,
   never an uncommitted worktree.
7. Follow [references/state-transitions.md](references/state-transitions.md)
   exactly. Stop on an invalid transition, `BLOCKED`, or
   `NEEDS_CLARIFICATION`.

## Stage 0 — resume guard and reset

1. Inspect GitHub for an open tool-delivery PR and inspect any live delivery
   state. If either says `AWAITING_MERGE`, verify merge or stop.
2. Resolve one issue/tool ID. Ambiguous duplicate IDs require clarification.
3. Verify the worktree is clean and update local `main` from `origin/main`.
4. Preview, then reset only workflow-owned child states:

   ```bash
   .venv/bin/python .codex/skills/drowai-tool-delivery-workflow/scripts/manage_tool_delivery_state.py \
     reset-child-states --repo-root .
   .venv/bin/python .codex/skills/drowai-tool-delivery-workflow/scripts/manage_tool_delivery_state.py \
     reset-child-states --repo-root . --apply --confirm-no-active-workflow
   ```

   Never reset cleanup, architecture-documentation, or unrelated program state.
5. Initialize delivery state with tool, issue, milestone, base, and the planned
   branch. Do not create the branch until capability analysis and guide
   creation justify implementation work.

## Stage 1 — capability decision

Run `$drowai-tool-capability-analysis`.

- `SUITABLE` or a bounded `NEEDS_FOUNDATION` → continue.
- `DEFERRED` / `NOT_PLANNED` → write an evidence-backed issue decision, attach
  it to the milestone, close with the appropriate reason when final, clean
  local child state, and stop. Do not create implementation code.
- Any ambiguity → `NEEDS_CLARIFICATION`.

Use Amass as the completed reference and reuse
`budget_rendered_items`; do not copy Amass-specific timeout policy.

## Stage 2 — guide creation and review

1. Call `implementation-guide-creator` with the exact tool, capability state,
   issue, and current code evidence. The guide lives under ignored
   `docs/devdocs/plan/`.
2. Create the planned topic branch from the recorded updated base, then record
   `BRANCH_READY`.
3. Run `$implementation-guide-review-loop` until `COMPLETE`.
4. Seed implementation state with the reviewed guide, full first task ID, and
   schema-2 lifecycle fields.

The ignored guide produces no empty “guide commit.”

## Stage 3 — phased implementation

Run `$feature-implementation-workflow`.

- One implementer invocation equals one guide task.
- Every phase boundary runs `$implementation-review-loop` in
  `current_phase` mode.
- After a phase review reaches `COMPLETE`, stage only that phase’s coherent
  tracked changes, run its focused checks, and commit.
- Continue until implementation state is terminal `COMPLETE`.
- Run `$implementation-review-loop` in `final_implementation` mode.

Any mechanical fix later returns through the affected implementation task and
fresh phase/final review before proceeding.

## Stage 4 — security and mechanical gates

1. Run `static-security-analyzer` over the branch diff, using the reviewed tool
   guide as intent context.
2. Record a secret-safe report reference/conclusion in delivery state.
3. Critical, High, or Medium findings—or any secret, authorization,
   task/workspace isolation, command-injection, unsafe-target, or
   runtime-provider-boundary issue—block. Fix through the implementation flow,
   rerun final review, then rerun security.
4. Run `$drowai-tool-mechanical-validation`.
5. `FAIL` returns to implementation. `INCONCLUSIVE` is allowed only for bounded
   model selection; resolve or explicitly record before PR. `NEEDS_CLEANUP`
   blocks.
6. Commit reviewed mechanical fixes, never reports.

## Stage 5 — committed-scope quality and bounded refactor

1. Ensure the worktree is clean and all implementation changes are committed.
2. Reset quality state from its example:
   - `scope.kind: branch`
   - `scope.target_ref`: current branch
   - `scope.base_ref: origin/main`
   - `scope.locked: false`
3. Run `$implementation-quality-review-loop` to `COMPLETE`.
4. Commit any bounded behavior-neutral quality fixes.
5. If quality creates a refactor suggestion:
   - automatically execute at most one same-PR refactor cycle, as requested;
   - create/review the refactor guide through the existing refactor and guide
     workflows;
   - implement/review/commit it;
   - rerun security, affected mechanical cases, original-guide final review,
     and a freshly reset quality review against the new branch head.
6. A second broad suggestion becomes a follow-up issue and does not block
   unless correctness/security requires it. Required broad work sets `BLOCKED`
   for a human scope decision.

## Stage 6 — final contribution gate

Run:

- all focused tool/parser/runtime/semantic/compression/knowledge tests;
- workflow contract tests;
- `python3 scripts/check_publication_safety.py`;
- `git diff --check`;
- frontend checks only when frontend contracts changed;
- a final original-guide implementation review after all refactors/fixes.

Update only affected canonical docs and add one concise `CHANGELOG.md`
`[Unreleased]` entry for the contributor-visible workflow/tool outcome. Never
change product versions.

## Stage 7 — publish and stop

1. Verify staged/tracked scope contains no ignored state, guide, reports,
   credentials, cookies, private targets, or browser artifacts.
2. Make the final coherent docs/gate commit.
3. Push and use the existing GitHub publishing capability to open a PR whose
   title follows `CONTRIBUTING.md` and whose body covers problem, solution,
   risk/security, exact validation, user-facing changes, AI assistance, and
   `Closes #<issue>`.
4. Attach the PR to Milestone #1.
5. Record `PR_OPENED` then `AWAITING_MERGE`.
6. Preserve the PR URL for the final response, then preview and apply
   `cleanup-workflow-states` with `--confirm-no-active-workflow`. Verify all
   workflow-owned live state files, including the delivery ledger, are absent.
   The next run must verify merge status from GitHub.
7. Stop. Do not begin another tool.

## Recovery

- Never skip a failed child gate by editing its status.
- Reinitialize review/quality state from the committed example before each
  fresh scope.
- If user input is required, persist the exact missing decision and stop.
- If an external write fails, preserve the code/commit state, record the failed
  action, and retry only that action.

## Validation

```bash
python3 /Users/gunesalcan/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/drowai-tool-delivery-workflow
.venv/bin/python -m pytest tests/codex_workflows -q
```
