<!--
Purpose: authoritative implementation reference for the behavior-preserving
subagent foundation quality refactor covering review items 2 through 8.
-->

# Subagent Foundation Quality Refactor

**Status:** Complete  
**Branch:** `codex/feat/multi-agent-orchestration`  
**Baseline commit:** `adfed7bc761b36c1d61631f0590bff4ce63b6600`  
**Scope:** Quality review items 2–8 only

## Goal and constraints

Simplify the current subagent implementation into a reusable foundation without
changing HTTP APIs, stream packets, persistence, prompts, routing decisions,
event ordering, lifecycle semantics, usage accounting, or user-visible output.

Every phase is move-and-delete work: reuse the existing authority, remove the
displaced implementation in the same phase, and keep no compatibility fallback,
parallel contract, duplicate composition root, test-only runtime path, or unused
helper. Each phase must pass its focused gates before the next phase starts.

Item 1 remains deferred. The broader historical proposals for handoff contract
authority, MessageList extraction, and presentation-store separation remain
reference material and are not active scope:

- [Agent Handoff Contract Authority](quality-refactor-agent-handoff-contract-authority.md)
- [Extract Agent-Run Integration from MessageList](quality-refactor-message-list-agent-run-integration.md)
- [Separate agent-run data and drawer presentation state](quality-refactor-agent-run-presentation-store.md)

## Issue decisions

### Item 2 — Parent-handoff guard lifecycle

**Problem:** Module-global task locks are retained indefinitely and inspect the
private `asyncio.Lock._loop` implementation detail.

**Reuse:** `ParentHandoffCoordinator` remains the guard responsibility owner;
`local_runtime.py` owns the shared production instance.

**Correction:** Add an injected, ref-counted `ParentHandoffGuardPool` beside the
coordinator. Count holders and waiters, remove idle keys, and clean up on normal,
error, timeout, and cancellation exits. Do not add a general concurrency module.

### Item 3 — Process-local runtime ownership

**Problem:** The backend has a shared registry/launcher but the chat composition
can create another default worker/launcher, leaving launch, cancellation, and
lifecycle publication with unclear ownership.

**Reuse:** `backend/services/agent_runs/local_runtime.py` is the sole production
composition root.

**Correction:** Build one lazy runtime bundle containing the registry, adapter,
executor, worker, launcher, publisher, and handoff guard pool. Production facades
use the bundle; explicit custom test composition remains isolated. Remove direct
hub fallbacks outside this composition boundary.

### Item 4 — Checkpoint continuation responsibilities

**Problem:** Generic checkpoint continuation embeds child-run attribution,
registry lifecycle, usage, publication, and parent-continuation decisions in one
large method.

**Reuse:** `backend/services/agent_runs/continuation.py` already owns subagent
resume validation and lifecycle transitions.

**Correction:** Keep checkpoint execution, parsing, hydration, persistence, and
generic result construction in `CheckpointContinuationService`. Move child-only
continuation work behind the existing agent-run continuation boundary and split
the generic method into execution, interrupted-result, and completed-result
helpers. Do not add a strategy framework or another continuation module.

### Item 5 — Parent-handoff coordinator size

**Problem:** One coordinator loop claims, waits, projects, decodes parent output,
dispatches follow-ups, publishes progress, mutates metadata, and settles claims.

**Reuse:** Existing result projection, ownership policy, dispatch, registry, and
event projection services remain authoritative.

**Correction:** Move parent-output decoding and stable decision identity into the
small `parent_control.py` contract module, then divide the coordinator loop into
focused private steps. The coordinator retains registry claim orchestration; no
second coordinator or workflow engine is introduced.

### Item 6 — Typed handoff data

**Problem:** Validated result and active-run projections become
`dict[str, Any]` while still inside the application workflow.

**Reuse:** The canonical graph context contracts `CompletedAgentResult` and
`ActiveAgentRun` define the JSON-facing shapes.

**Correction:** Use those types throughout projection, coordinator callbacks,
and progress building. Add the already-emitted `agent_id` field to
`CompletedAgentResult`. Convert to ordinary JSON collections only at metadata or
stream serialization boundaries. Do not create another projection model.

### Item 7 — Subagent graph construction

**Problem:** Production uses an explicit task-checkpointer builder while an
exported cached/default-checkpointer API exists only for tests.

**Reuse:** The current subagent graph module remains the sole topology and
compilation authority.

**Correction:** Expose one uncompiled topology constructor and one compiler that
requires an explicit checkpointer. Remove the cached getter, definition
fingerprinting, related registry imports, and cache-only tests.

### Item 8 — Frontend agent-run protocol authority

**Problem:** Lifecycle literals and subagent identification are repeated across
chat rendering, grouping, stream compatibility, and stores; one compatibility
check is hard-coded to `agent_kind == "recon"`.

**Reuse:** `client/src/features/agent-runs/contracts/agent-run.ts` remains the
single frontend protocol authority.

**Correction:** Add reusable lifecycle, attribution, parent-control, and generic
metadata readers there. Replace local detectors and hard-coded literals while
leaving marker placement, transcript layout, and drawer behavior in their
current presentation owners. Do not perform the broader MessageList extraction.

## Phase tracker

| Phase | Work | Status | Verification |
|---|---|---|---|
| 0 | Baseline and reference | Complete | Baseline recorded below |
| 1 | Process-local runtime authority | Complete | Runtime/facade/worker/continuation gate passed |
| 2 | Parent-handoff guard lifecycle | Complete | Cross-coordinator and cleanup contracts passed |
| 3 | Typed handoff data | Complete | Projection/coordinator/E2E contracts passed |
| 4 | Parent-control extraction and coordinator split | Complete | 35 focused tests passed |
| 5 | Continuation responsibility split | Complete | 52 focused tests passed |
| 6 | Graph construction unification | Complete | 31 focused tests passed |
| 7 | Frontend protocol authority | Complete | Historical gate retained; follow-up envelope correction passed |
| 8 | Cleanup and integration gate | Complete | 203 backend tests, type-check, build, and diff checks passed |

## Completion criteria

- One production process-local runtime owns launch and cancellation objects.
- No direct lifecycle publication fallback exists outside the composition root.
- Parent handoff guards are injected, task-scoped, and removed when idle.
- Handoff result and active-run collections retain canonical types internally.
- Parent-control decoding has one implementation and the coordinator remains a
  claim-orchestration service.
- Generic continuation no longer implements child-run lifecycle details inline.
- All subagent graphs compile through the explicit task-checkpointer path.
- Frontend agent-run protocol recognition has one generic authority.
- Focused and combined gates pass with unchanged serialized outputs and UI.

## Verification log

No temporary characterization tests remain. The new parent-control and guard
tests protect enduring parsing and concurrency contracts; cache-only tests were
deleted with the cached graph route.

### Baseline

- Baseline branch and commit: `codex/feat/multi-agent-orchestration` at
  `adfed7bc761b36c1d61631f0590bff4ce63b6600`.
- Focused backend baseline: 170 passed, 3 warnings.
- Focused frontend baseline: 97 passed with one existing failure:
  `MessageList.agent-runs.test.tsx` expected a replayed nonterminal local run to
  become interrupted. The backend envelope includes `process_local`, while the
  existing strict frontend list schema did not accept that field. The follow-up
  contract correction now accepts only `process_local: true`, and the replay
  reconciliation test passes with the backend-defined interrupted state.

### Phase gates

- Runtime authority, lifecycle publication, handoff guards, typed projections,
  and continuation: focused gates passed throughout; the final continuation
  gate passed 52 tests.
- Parent-control parsing, coordinator claim lifecycle, and subagent E2E:
  35 passed.
- Canonical graph topology, explicit compilation, worker, continuation, and
  subagent E2E: 31 passed.
- Frontend agent-run contracts, store/replay, MessageList, activity grouping,
  message grouping, compatibility filtering, chat-stream terminalization, and
  stream ingestion retained the historical phase gate. The focused follow-up
  contract, replay, and MessageList gate passed 27 tests after the envelope
  correction.
- TypeScript: `npm run check` passed.
- Production frontend: `npm run build` passed (existing large-chunk warning).

### Final integration

- Combined affected backend agent-run, facade, checkpoint, HITL, graph-name,
  and subagent E2E suite: 203 passed, 3 dependency deprecation warnings.
- `git diff --check`: passed.
- A broader optional HITL prompt-regression file has four unrelated failures
  because its `_CheckpointerWithSetup` test double is rejected by the installed
  LangGraph version. No code in that checkpointer or graph-builder path changed
  in this refactor.
- The final source search found no cached subagent graph API, definition cache
  fingerprint, module-global handoff guard, private lock-loop inspection,
  direct lifecycle hub fallback, duplicated frontend lifecycle literal, or
  recon-specific frontend routing.
