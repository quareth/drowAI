---
name: implementation-quality-review-loop
description: Run DrowAI's automated state-driven implementation code-quality reviewer/fixer loop for one strict frozen Git scope recorded in `.codex/agents/implementation-quality-review-state.md`. Use when the user supplies a branch name or commit id and asks for quality assessment, behavior-neutral cleanup, DRY/modularity/separation-of-concerns review, removal of scoped unused/dead/residual/duplicated code, or a final quality gate without changing implementation behavior.
---

# Implementation Quality Review Loop

Run a quality-only fresh reviewer -> surgical fixer -> fresh reviewer loop. Assess exactly one branch or commit scope, apply only obvious small-to-medium behavior-neutral cleanup, and record large refactors as non-blocking suggestions under `docs/devdocs/refactor/`.

## Durable files

- `.codex/agents/implementation-quality-review-state.md`: strict Git scope, frozen resolution, current-cycle findings, coverage, and refactor suggestion paths.
- `.codex/agents/implementation-quality-review-state.example.md`: clean initialization template.
- `.codex/agents/implementation-state.md`: optional intent context only; never a quality-scope authority.

## Strict Git scope

Require state `scope.kind` and `scope.target_ref`:

- `branch`: inspect all implementation changes on `target_ref` since its merge base with required `scope.base_ref`.
- `commit`: inspect only the changes introduced by exact `target_ref` against its first parent.

On the first reviewer pass, resolve refs to immutable SHAs, compute the diff range and changed-file list, persist them under `scope.resolved`, and set `scope.locked: true`. Every later reviewer and fixer must use that frozen resolution and never re-resolve refs. Never expand scope from the guide, implementation-state, current working-tree diff, adjacent code, or later branch commits.

Read surrounding code and wired callers only for context. Findings must identify code introduced or materially changed by the frozen diff. Fixes may edit only non-deleted paths in `scope.resolved.changed_files`; do not create new implementation files or edit contextual files outside the frozen list.

## Quality-only boundary

Include maintainability concerns only: responsibilities, cohesion, architectural placement, DRY, modularity, coupling, simplicity, abstractions, readability, complexity, scoped residual code, module docstrings, test-code quality, and type/interface clarity.

Prioritize behavior-neutral cleanup:

- remove unused imports, variables, branches, helpers, and files introduced by the scope;
- remove dead or residual compatibility/temporary code introduced by the scope;
- remove or refine scoped duplication by using an established authority without modifying that authority;
- simplify scoped internal structure, names, and tests without changing results or contracts.

Exclude security, functional correctness, feature completeness, acceptance criteria, performance, guide quality, repository-wide dead-code discovery, and pre-existing unrelated debt. Do not change features, algorithms, public APIs, persistence contracts, security boundaries, runtime behavior, or the approved implementation approach.

## New-chat simulation rule

Every cycle must simulate:

```text
new chat -> full frozen-scope quality review -> bounded findings -> fix -> new chat -> full frozen-scope quality review
```

- Spawn a fresh `implementation-quality-reviewer` for every review pass.
- Do not pass prior findings, fixer reports, or chat history.
- The fixer must clear `active_findings` before the next reviewer.
- Preserve only the frozen Git scope, neutral metadata, round counter, coverage, and non-blocking refactor suggestion paths.

## Workflow

1. Initialize or fully reset quality state from the example.
2. Set `scope.kind`, `scope.target_ref`, and branch-only `scope.base_ref` before running the reviewer.
3. Spawn a fresh `implementation-quality-reviewer`; it resolves and freezes scope when `scope.locked` is false.
4. Route by state:
   - `REVIEW_BLOCKED`: call `implementation-quality-fixer` immediately.
   - `READY_FOR_REVIEW`: spawn a fresh quality reviewer immediately.
   - `COMPLETE`: stop this loop and return control to the parent workflow.
5. After the fixer resets state to `READY_FOR_REVIEW`, spawn a new reviewer with no pasted context.
6. Continue until `COMPLETE`. Every candidate must resolve automatically as a bounded cleanup, a non-blocking refactor suggestion, or no finding.

To review a different branch or commit later, start a new run by resetting the state. Never replace `target_ref` or `base_ref` while `scope.locked: true`.

## Fixability rule

Only evidence-backed, small-to-medium, behavior-neutral cleanup inside the frozen changed-file set may enter `active_findings`.

If safe resolution requires broad redesign, cross-component migration, out-of-scope edits, public contract or schema changes, widespread caller edits, staged extraction, new source modules, or uncertain behavior preservation:

1. Do not block the loop.
2. Do not perform the refactor.
3. Create or update `docs/devdocs/refactor/quality-refactor-<short-slug>.md`.
4. Record only the path under state `refactor_suggestions`.
5. Continue with remaining bounded findings.

## State routing

- `READY_FOR_REVIEW` -> fresh `implementation-quality-reviewer`
- `REVIEW_BLOCKED` -> `implementation-quality-fixer`
- `COMPLETE` -> stop quality loop; parent workflow continues

No clarification, manual-stop, or fixer-complete steady state is valid.

## Hard rules

- Treat the frozen branch/commit diff as the sole implementation scope.
- Assess quality and clean internal dirt; do not change behavior or reimplement the feature.
- Do not absorb security or completeness work.
- Do not let the reviewer edit implementation code.
- Do not let the fixer edit outside frozen changed files or create implementation files.
- Do not allow large refactors.
- Do not preserve old findings or fix history.
- Do not create routine inspection reports; only large-refactor suggestions are durable documents.
- Do not stop between automatic reviewer/fixer transitions.

## Parent workflow handoff

In program execution, default to `scope.kind: branch`, the current checked-out branch as `target_ref`, and `origin/main` as `base_ref`. Run this loop after final implementation review and before `advance_queue`.

## Final response

Report the resolved target SHA and diff range, reviewed and skipped files, behavior-neutral cleanup and verification, refactor suggestion paths, final status, and whether control returned to a parent workflow.
