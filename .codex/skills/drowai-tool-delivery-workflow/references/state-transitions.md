# One-tool delivery state transitions

## Normal path

```text
READY
  -> ANALYZING
  -> BRANCH_READY
  -> GUIDE_REVIEW
  -> IMPLEMENTING
  -> IMPLEMENTATION_REVIEW
  -> SECURITY_REVIEW
  -> MECHANICAL_VALIDATION
  -> QUALITY_REVIEW
  -> FINAL_REVIEW
  -> READY_FOR_PR
  -> PR_OPENED
  -> AWAITING_MERGE
  -> MERGED
```

`QUALITY_REVIEW -> REFACTORING -> SECURITY_REVIEW` is allowed once. The new
cycle must rerun affected mechanics, original-guide final review, and quality
from an unlocked state against the new branch head.

## Decision path

```text
ANALYZING -> DECISION_PENDING -> DECISION_RECORDED
```

Use it for `NOT_PLANNED` or final `DEFERRED` outcomes. The issue must contain
the evidence/reason and milestone attachment before `DECISION_RECORDED`.

## Stop paths

Any active stage may enter `BLOCKED` or `NEEDS_CLARIFICATION`. Resume only to
the recorded `resume_status` after the condition is resolved.

## Invalid transitions

- Any new tool while status is not `MERGED` or `DECISION_RECORDED`.
- Any next tool without a positive GitHub classification of the prior delivery
  as `none`, `merged`, or `decision_recorded`.
- Any next tool while GitHub has an open prior delivery PR, or a closed-unmerged
  PR without a final linked-issue decision.
- Implementation before guide review is `COMPLETE`.
- Security pass without a report reference, acceptable conclusion, and zero
  blocking findings.
- Any gate transition whose owning child workflow has not completed.
- Security, mechanics, quality, final implementation, or final tests that
  predate the latest code-changing commit.
- Quality review with uncommitted implementation changes or a locked stale
  scope.
- `READY_FOR_PR` while any required gate is not `passed`.
- More than one automatic same-PR refactor cycle.
- Automatic refactor work that expands the accepted issue/guide, public API,
  schema/migrations, architecture boundaries, or unrelated components.
- Resetting or deleting a nonterminal state owned by another workflow.

## Gate order

| Gate | Pass authority | On failure |
|---|---|---|
| Capability | capability-analysis state | decision or clarification |
| Guide | guide-review state `COMPLETE` | fresh fixer/reviewer cycle |
| Phase/final correctness | implementation-review state | implementation fixer |
| Security | static analyzer report summarized in delivery state | implementation flow, then re-review |
| Mechanics | mechanical-validation state/report | implementation flow or cleanup |
| Quality | frozen committed branch scope | quality fixer/refactor route |
| Final | original guide plus all required commands | return to owning gate |

Record the reviewed code commit for the final correctness, security,
mechanical, and quality gates. Any later code-changing commit invalidates them
and routes back through those reviews.

## Commit boundaries

- Reviewed implementation phase.
- Reviewed mechanical/correctness fixes.
- Bounded quality cleanup.
- Same-PR refactor cycle, when used.
- Final canonical docs/changelog.

Ignored guides or state resets never justify an empty commit.

## GitHub bookkeeping

The milestone bootstrap is a separate authorized repository-maintenance action:

- attach merged Amass PR as the completed reference;
- attach the workflow foundation issue;
- create/attach one evaluation issue for each remaining selected tool;
- record Masscan as a closed not-planned/deferred decision;
- attach each delivery PR to Milestone #1.

During a per-tool run, mutate only its existing issue/PR unless the user
explicitly expands external-write scope.
