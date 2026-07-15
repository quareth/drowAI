Use the **implementation-review-loop** skill in **Final Implementation Review** mode.

Review the completed implementation against the guide in `.cursor/agents/implementation-state.md`, unless the user names a different guide or scope.
Ignore `phase` and `task`; those are only for feature-implementer/current-task review.
Initialize review-state with `mode: final_implementation`, `phase: ""`, `task: ""`, and full-guide scope.
Do not call `feature-implementer`.
Run reviewer -> fixer -> fresh reviewer until no blockers remain, `MAX_ROUNDS_REACHED`, or `NEEDS_CLARIFICATION`.
Use `.cursor/agents/implementation-review-state.md` as the blocker ledger; do not paste full reports between agents.
