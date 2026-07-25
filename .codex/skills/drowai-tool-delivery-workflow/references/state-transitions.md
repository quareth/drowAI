# One-tool delivery state transitions

## Normal path

```text
READY
  -> BRANCH_READY
  -> ANALYZING
  -> GUIDE_REVIEW
  -> IMPLEMENTING
  -> IMPLEMENTATION_REVIEW
  -> MECHANICAL_VALIDATION
  -> QUALITY_REVIEW
  -> FINAL_REVIEW
  -> READY_FOR_PR
  -> PR_OPENED
```

`PR_OPENED` is terminal for this run; clean ignored state and stop without
waiting for merge.

## Decision path

```text
ANALYZING -> DECISION_PENDING -> DECISION_RECORDED
```

Use this path for final `DEFERRED` or `NOT_PLANNED` outcomes, record the
evidence and concise reason on only the selected milestone-list entry, preserve
the rest of the description, clean state, and stop.

## Fix and refactor loops

```text
MECHANICAL_VALIDATION -> IMPLEMENTING
QUALITY_REVIEW -> REFACTORING -> IMPLEMENTATION_REVIEW
IMPLEMENTATION_REVIEW -> MECHANICAL_VALIDATION -> QUALITY_REVIEW
```

A refactor suggestion is executed directly only when every modified path is
already present in `git diff --name-only origin/main...HEAD`; no refactor guide
is created.

## Stop paths

Any active stage may enter `BLOCKED` or `NEEDS_CLARIFICATION`, and it may resume
only to the recorded `resume_status` after the condition is resolved.

## Invalid transitions

- Running an agent before the tool branch exists.
- Reviewing or modifying a different branch, base, milestone entry, or tool.
- Requiring a per-tool issue before selecting a milestone-list entry.
- Selecting an entry already marked terminal or represented by an attached open
  tool PR.
- Treating an unrelated milestone issue or unmarked PR as delivery evidence for
  a selected tool.
- Creating the implementation guide before current registered/visible tools
  are discovered and mature references are selected per responsibility.
- Using Amass or any other fixed tool as a universal implementation reference.
- Duplicating Amass budgeting instead of reusing
  `budget_rendered_items`, or copying its unrelated tool-specific behavior.
- Creating the implementation guide before the real Kali installation and
  official CLI-contract gates pass.
- Treating a host package, image manifest, or repository wrapper as proof that
  the executable exists inside the selected task Kali runtime.
- Applying latest official CLI flags to an older installed version without
  checking its versioned help/manual.
- Expanding an obvious pre-implementation definition correction into new
  modules, architecture, or unrelated files.
- Starting the next phase before the successful current phase is committed.
- Implementing before the guide review reaches `COMPLETE`.
- Entering quality review with uncommitted implementation changes.
- Executing a refactor against a file absent from the branch diff allowlist.
- Treating prompt or prose quality as a mechanical failure.
- Opening the PR before implementation, mechanics, quality, and final checks
  complete.
- Waiting for merge or starting another tool after `PR_OPENED` in the same run.
- Resetting or deleting a nonterminal state owned by another workflow.

## Gate ownership

| Gate | Authority | Failure route |
|---|---|---|
| Milestone selection | current description plus attached PR markers | reconcile and choose one pending entry |
| Repository references | capability state plus current wired registry/visibility evidence | rediscover and select per responsibility |
| Kali installation | capability state plus provider-mediated task-runtime evidence | defer and return to selection |
| Official CLI contract | versioned primary sources plus installed help/manual | direct obvious correction or guide gap |
| Capability | capability-analysis state | decision or clarification |
| Guide | guide-review state `COMPLETE` | guide fixer/reviewer loop |
| Phase | implementation-review state | implementation fixer |
| Mechanics | mechanical-validation state | implementation or cleanup |
| Quality | frozen `origin/main...HEAD` scope | quality fixer or direct branch refactor |
| Final | original guide plus focused commands | return to owning gate |

## Commit boundaries

- Obvious selected-tool definition drift corrected before guide creation.
- Every successful implementation phase before the next phase starts.
- Reviewed mechanical fixes and verified user-guide corrections.
- Bounded quality fixes.
- Each accepted branch-scoped refactor.
- Final canonical documentation and changelog updates.

Ignored guides, state files, reports, and refactor suggestions never justify a
commit.

## GitHub bookkeeping

- Use the milestone description's `Selected tools` list as the backlog.
- Use attached PRs with explicit milestone-tool and tool-ID markers as delivery
  evidence; issues are optional context only.
- For a final deferred/not-planned decision, update only the selected milestone
  list entry with a concise evidence-backed status and reason.
- Attach the completed delivery PR to Milestone #1.
- End the run immediately after push, PR creation, attachment, and state
  cleanup.
