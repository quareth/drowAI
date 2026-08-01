<!-- Purpose: Record extraction of agent-run orchestration from the generic chat transcript. -->

# Agent-Run Transcript Integration Boundary

**Status:** Complete

**Implementation commit:** `94e9891` (`refactor(frontend): extract agent run transcript integration`)

## Completed boundary

`client/src/features/agent-runs/components/AgentRunTranscriptIntegration.tsx`
is the feature-to-chat integration boundary. It owns local-status hydration,
synthetic lifecycle markers, group-to-run matching, card substitution,
cancellation, presentation-store interaction, resizable layout, and drawer
rendering.

`MessageList` retains pagination, grouping, scrolling, unread state, retry
behavior, empty/loading states, and generic transcript rendering. Its only
feature extension is an optional generic activity-group render slot. The normal
chat composition renders the feature integration component.

## Preserved contracts

- Parent/child filtering, marker metadata, ordering, task isolation, and local
  replay hydration are unchanged.
- Drawer activation, cancellation, approval controls, layout sizing, scrolling,
  retries, and ordinary transcript rendering are unchanged.
- The generic transcript has no direct agent-run API, store, card, drawer, or
  cancellation dependency.

## Verification evidence

- Agent-run transcript tests moved to the feature integration suite.
- Generic MessageList activity tests pass independently of agent runs.
- Agent-run card/drawer, TurnActivityCard, grouping, stream-ingestion, cleanup,
  and overview-shell suites passed in a 111-test focused gate.
- `npm run check` and `npm run build` passed.
- Source searches confirmed that `MessageList.tsx` contains only the generic
  render-slot interface from this integration.
