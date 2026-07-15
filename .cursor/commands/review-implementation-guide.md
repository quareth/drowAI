Use the **implementation-guide-review-loop** skill.

Run blocker-only review/fix loop for the implementation guide defined in `.cursor/agents/implementation-guide-state.md`.
This loop reviews and fixes the guide document itself, not code implementation.

Use fresh reviewer cycles:
`@implementation-guide-reviewer` -> `@implementation-guide-fixer` -> fresh `@implementation-guide-reviewer`

Stop when `COMPLETE`, `MAX_ROUNDS_REACHED`, or `NEEDS_CLARIFICATION`.
