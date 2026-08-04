<!-- Purpose: Record the completed separation of agent-run data and presentation state. -->

# Separate Agent-Run Data and Presentation State

**Status:** Complete

**Implementation commit:** `1c46e69` (`refactor(frontend): separate agent run presentation state`)

## Completed boundary

`client/src/features/agent-runs/state/agent-run-presentation-store.ts` owns
drawer view/open state, selected run, parent anchor, activity expansion,
presentation actions/listeners, and bounded task-keyed persistence.

`agent-stream-store.ts` retains lifecycle records, replay merging, activity
limits, ordering, and reconciliation. Presentation-only types and closed-state
constants no longer live in the stream protocol contract. Task cleanup clears
both stores through their focused APIs.

## Preserved contracts

- Lifecycle ordering, activity deduplication and bounds, reconciliation, and
  task isolation are unchanged.
- Drawer navigation, selection, expansion, and closed-by-default behavior are
  unchanged.
- Presentation mutations do not rebuild or notify data snapshots; stream
  ingestion does not mutate or notify presentation snapshots.
- No general-purpose store framework or additional state authority was added.

## Verification evidence

- Presentation-store isolation, navigation, selection, expansion, cleanup, and
  stable-snapshot tests passed.
- Stream-store, replay, drawer, card, cleanup-hook, and overview-shell suites
  passed in a 62-test focused gate.
- TypeScript checking and the generated test inventory passed.
- Source searches found no drawer/presentation ownership remaining in the
  stream store.
