Use the **feature-implementation-workflow** skill.

Run the state-driven implementation workflow from `.cursor/agents/implementation-state.md`, or from the guide/phase/task named by the user.

Continue automatically:
`@feature-implementer` task-by-task within phase -> `implementation-review-loop` Current Phase Review at phase boundary -> `@feature-implementer next`

Stop only when the guide is complete, `MAX_ROUNDS_REACHED`, `NEEDS_CLARIFICATION`, `advance_after_complete: false`, or the user stops.
