<!--
Purpose: authoritative completion record for the behavior-preserving subagent
foundation quality series.
-->

# Subagent Foundation Quality Refactor

**Status:** Complete

**Branch:** `codex/feat/multi-agent-orchestration`

**Series base:** `2100985`

**Scope:** Reusable subagent and agent-run foundation quality

## Outcome

The subagent implementation now has explicit authorities for definition
metadata, process-local lifecycle settlement, handoff contracts, prompt
components, frontend data and presentation state, transcript integration, and
shared contract test data.

Serialized HTTP payloads, stream packets, prompt output, lifecycle ordering,
usage accounting, task isolation, and user-visible layout remain unchanged.
The only intentional observable correction is acceptance of backend
`process_local: true` agent-run envelopes by the strict frontend contract.

## Completed series

| Commit | Change | Result |
|---|---|---|
| `ebbbf9b` | Accept process-local agent-run envelopes | Frontend list/cancel contracts match the backend marker; replayed nonterminal local runs reconcile to `interrupted` |
| `2c7561b` | Make the definition registry authoritative | Dispatch, completion, projections, continuation, and interrupt inspection use the injected registry |
| `55c7ac5` | Settle lifecycle before task completion | Awaiting a launcher task now guarantees terminal/waiting registry state and published lifecycle ordering |
| `24bb152` | Remove the PTR test-only decision path | Production and tests use the provider-native visible-text plus route-tool seam |
| `bca203f` | Centralize the handoff contract | Model, normalization, collection policy, and strict schema share one domain authority |
| `c005e9d` | Compose shared native-tool guidance | Main and subagent prompts structurally compose one versioned guidance component |
| `1c46e69` | Separate presentation state | Stream data and drawer/navigation state have independent stores and listeners |
| `94e9891` | Extract transcript integration | Agent-run orchestration lives in the feature boundary; `MessageList` is generic |
| `39e307b` | Centralize contract test builders | Backend and frontend suites share fresh linked contract builders without shared scenario state |

## Authority map

- Definitions and display metadata:
  `agent/subagents/registry.py` and injected `SubagentRegistry` instances.
- Process-local composition and lifecycle:
  `backend/services/agent_runs/local_runtime.py`, `launcher.py`, and
  `registry.py`.
- Handoff shape, normalization, and schema:
  `agent/subagents/handoff.py`.
- Shared native-tool guidance:
  `core/prompts/versions/tool_planning/v7/native_tool_call_guidance.txt`,
  resolved through `TemplateLoader` and the tool-planning builder.
- Agent-run stream data:
  `client/src/features/agent-runs/state/agent-stream-store.ts`.
- Agent-run presentation state:
  `client/src/features/agent-runs/state/agent-run-presentation-store.ts`.
- Chat integration:
  `client/src/features/agent-runs/components/AgentRunTranscriptIntegration.tsx`.
- Contract test data:
  `backend/tests/agent_run_test_support.py` and
  `client/src/features/agent-runs/test-data.ts`.

## Design guarantees

- Registries are injected at composition boundaries; no definition or service
  object enters serialized graph state or checkpoints.
- Worker completion, registry settlement, and lifecycle publication are owned
  by one launcher task; no detached completion observer or polling spin remains.
- Handoff callers retain only layer-specific orchestration and error
  translation.
- Prompt families resolve structural components instead of parsing rendered
  prose.
- Stream and presentation stores have independent snapshots and notification
  paths.
- The generic transcript depends only on a generic activity render slot.
- Test builders create fresh contracts and derive assignment/result identities
  from their owning inputs.
- No compatibility fallback, generic framework, abstract base class, or second
  authority was introduced.

## Verification record

- Envelope contract/replay, backend router, and MessageList regression tests
  pass; the previous replay failure is resolved by the strict marker contract.
- Custom-registry dispatch, completion, projection, continuation, and interrupt
  inspection tests pass while default Pathfinder payloads remain unchanged.
- Launcher success, failure, pause, cancellation, publication-failure, and
  attach-failure contracts pass with preserved lifecycle ordering.
- PTR, handoff, prompt, agent-run service, facade, handler, continuation,
  interrupt, and subagent E2E focused suites pass.
- Prompt verification includes byte-for-byte main and subagent golden output.
- Presentation and transcript gates pass independently for data state,
  presentation state, generic transcript behavior, and feature integration.
- The latest contract-builder gate passed 159 backend tests and 56 frontend
  tests; TypeScript checking and the 1,230-file generated inventory passed.
- Frontend production builds completed successfully after the transcript
  boundary extraction.

## Related completion records

- [Agent Handoff Contract Authority](quality-refactor-agent-handoff-contract-authority.md)
- [Shared Native-Tool Guidance Composition](quality-refactor-tool-planning-shared-guidance.md)
- [Post-Tool Reasoning Test Compatibility Seam](quality-refactor-post-tool-test-compatibility-seam.md)
- [Separate Agent-Run Data and Presentation State](quality-refactor-agent-run-presentation-store.md)
- [Agent-Run Transcript Integration Boundary](quality-refactor-message-list-agent-run-integration.md)
- [Agent-Run Contract Test Builders](quality-refactor-agent-run-test-data-builders.md)
