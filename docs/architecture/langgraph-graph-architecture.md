# LangGraph Graph Architecture

Code-verified overview of the active LangGraph branch selection, graph builders,
node topology, checkpoint/stream execution, and graph boundaries.

## Purpose

LangGraph is the per-turn workflow runtime for task chat. It routes each turn
into one of four graph branches:

- normal chat
- simple tool execution
- deep reasoning
- subagent handoff (currently Pathfinder recon)

The backend facade owns branch selection. Graph builders own node topology.
Graph nodes own local state transitions and emitted stream events.

## Checkpointer Schema Lifecycle

PostgreSQL checkpointer schema DDL is startup-owned. The FastAPI lifespan runs
LangGraph's idempotent checkpointer setup before background services start and
before the application accepts traffic. A PostgreSQL session advisory lock
serializes this bootstrap across concurrent backend replicas.

Request paths only acquire an already-initialized checkpointer. Chat history
prewarm and graph handlers do not run schema setup or index creation. Chat
history, prewarm, and readiness endpoints release their short ORM transactions
before awaiting runtime warmup, preventing PostgreSQL concurrent-index setup or
other external work from retaining request connections.

## Responsibility Boundary

Owned by LangGraph graph architecture:

- Graph state shape and node topology.
- Capability gates at graph entry.
- Prompt-context consumption through runtime metadata.
- Tool planning and execution subgraph wiring.
- HITL interrupt points and checkpoint-compatible resume shape.
- Stream event emission through LangGraph custom events.

Not owned by graph builders:

- HTTP request validation.
- Credential storage/decryption.
- Task ownership/tenant authorization.
- Runtime provider selection.
- Chat row reservation and final persistence.
- Frontend rendering decisions.

## Wired Entrypoints

- `backend/services/langgraph_chat/facade.py`
  - `LangGraphChatFacade.handle_turn` builds runtime config, enriches intent,
    resolves branch, and delegates to the selected handler.
- `backend/services/langgraph_chat/routing/selectors.py`
  - Maps `ExecutionMode` to normal chat, simple tool, or deep reasoning.
- `backend/services/langgraph_chat/handlers/normal_chat_handler.py`
  - Compiles `build_simple_chat_graph`.
- `backend/services/langgraph_chat/handlers/simple_tool_handler.py`
  - Compiles `build_simple_tool_graph`.
- `backend/services/langgraph_chat/handlers/deep_reasoning_handler.py`
  - Compiles `compile_deep_reasoning_graph`.
- `backend/services/langgraph_chat/handlers/subagent_handler.py`
  - Validates classifier handoff arrays, launches generic subagent runs, and
    hands terminal child results to the parent handoff coordinator.
- `backend/services/agent_runs/parent_handoff_coordinator.py`
  - Claims ready process-local child results, snapshots active runs, serializes
    parent continuation, and applies or releases claims through the registry.
- `agent/graph/builders/parent_handoff_builder.py`
  - Compiles the parent continuation graph that routes claimed child handoffs
    through post-action reasoning before finalization or backend-owned parent
    controls.
- `backend/services/langgraph_chat/execution/graph_executor.py`
  - Runs compiled graphs with `astream(..., stream_mode=["custom", "values"])`.
- `agent/graph/graph_builder.py`
  - Simple chat and minimal/bootstrap graph builders.
- `agent/graph/builders/simple_tool_builder.py`
  - Simple-tool graph builder.
- `agent/graph/builders/deep_reasoning_builder.py`
  - Deep-reasoning graph builder.

## Branch Selection

```mermaid
flowchart TD
    ChatAPI[Chat submit]
    Context[Runtime config builder]
    Intent[Intent classifier]
    Selector[Branch selector]
    Normal[NormalChatHandler]
    Simple[SimpleToolHandler]
    Deep[DeepReasoningHandler]
    Subagent[SubagentHandler]

    ChatAPI --> Context
    Context --> Intent
    Intent --> Selector
    Selector --> Normal
    Selector --> Simple
    Selector --> Deep
    Selector --> Subagent
```

Selection inputs:

- normalized `agent_mode`
- `plan_mode`
- deployment-aware provider/model/runtime metadata
- intent classifier enrichment
- ordered classifier `agent_handoffs`
- feature flags for simple-tool and deep-reasoning availability
- deterministic test mode overrides

Route policy:

- Chat mode forces normal chat.
- Plan overlay forces deep reasoning while preserving the underlying autonomy
  mode for approval behavior.
- Agent/full-access without plan mode can route through classifier-derived
  execution mode.
- A direct-executor turn with supported required Pathfinder handoff entries
  routes to the generic subagent handler. `suggested_capabilities` remains
  advisory assignment context and is not delegation authority.
- `agent/subagents/registry.py` is the source of truth for enabled declarative
  subagent definitions and their classifier-safe projections: names, purpose,
  ownership boundary, supported and excluded task categories, target
  requirements, and per-task concurrency limits.
- The intent prompt projects `registry.classifier_catalog()` at runtime, and
  the structured-output schema constrains `subagent` to `registry.ids()`.
  Deterministic routing validates each emitted name through the same registry
  and fails closed when the name, live availability, or target requirements do
  not match. Valid plans dispatch through the single generic `subagent` facade
  branch.
- Because the registry is currently small and process-static, the classifier
  receives direct catalog enumeration. Tool-based agent discovery is reserved
  for a materially larger or dynamically managed registry.
- The classifier contract represents handoffs as an ordered array. The handler
  validates every handoff against the live registry before launching any run,
  caps the plan through `MAX_AGENT_HANDOFFS`, runs allowed entries in
  concurrency-limited ordered batches, and preserves one immutable run/thread
  per invocation. Terminal child results are claimed from the process-local
  registry and enter parent post-action reasoning before the parent can
  finalize, delegate follow-up work, call a direct tool, think, reflect, or
  wait for still-active child runs.

### Pre-classifier context compaction

For non-deterministic turns, `LangGraphChatFacade` resolves the classifier's
V2 deployment runtime selection, prepares the exact classifier request, and
counts that request's system prompt, user prompt, structured-output schema, and
output reservation before classification. The soft trigger is
`floor((context_limit - reserved_output_tokens) * 0.80)`; the optional
`LANGGRAPH_CONTEXT_COMPACTION_TRIGGER_TOKENS_OVERRIDE` changes only that trigger
when it is positive and below the usable prompt budget. It does not change the
model's hard context limit. Deterministic mode bypasses both classification and
this compaction decision.

When compaction triggers, every compressor pass uses the same V2 deployment
runtime selection through the existing runtime client resolver. Legacy
provider/model checkpoints are not written for new turns; serializable graph
state retains deployment references and compatibility snapshots only. For each
retained-tail candidate, the facade rebuilds and recounts the exact candidate
classifier request through `IntentClassifier.prepare_request`; only a
candidate that fits the hard limit is persisted and installed, and that
already-counted request is the object sent to the classifier. Provider failures
are not compatibility-resubmitted for compatibility. Each compressor provider
request receives at
most one short jittered retry for provider-neutral timeout, rate-limit, or
temporary upstream failures; invalid output, refusal, authorization,
configuration, request-fit, and persistence failures are not retried. This
provider retry is distinct from the compressor's conditional second corrective
pass and the bounded five/four/three whole-turn candidate sequence. If
compaction fails after the soft trigger while the original prepared classifier
request still fits the model's hard limit, the failed lifecycle and warning
metadata are emitted and the unchanged prepared request continues. The turn
fails before classification only when that original request cannot fit.

The single conversation-bundle builder always installs a required
`classifier_transcript_window` from the full canonical history loaded for the
turn. Immediately before non-deterministic classification, the facade replaces
only that field when a compacted candidate is validated and persisted. The
intent-classifier projection never falls back to the shared bounded window and
invokes the exact full-or-compacted request that was accounted. Planner,
category-selection, and post-action reasoning continue to read the unchanged
shared bounded `transcript_window`.
Candidate validation reduces only by complete turns from five through three;
if the three-turn candidate still exceeds the selected model's hard limit, the
facade exits before classifier invocation with `context_uncompactable`.
Validated snapshots keep their epoch, source-token count, and exact positive
`through_message_id` cutoff together in the existing
`citations["context_compression"]` payload; no additional storage table is
introduced. Snapshot rows are reserved with no raw-message parent, so writing a
summary does not alter any raw row's parent or latest-child relationship. On
reload, the reader verifies the snapshot and cutoff are in the requested
task/conversation, locates that cutoff in its canonical raw traversal, and
reconstructs the prompt as the summary plus every raw message after the cutoff;
the snapshot row's physical position does not define the retained window. This
projection does not select an active branch: sibling subtrees keep the reader's
existing depth-first, created-time order, and new raw turns continue to parent
from the canonical raw-history tail. If the newest summary has missing or
malformed metadata, a foreign cutoff, or a cutoff absent from that traversal,
the reader ignores summary rows and projects the complete raw canonical history;
it never uses physical summary position or an older snapshot to drop raw rows.
The active user turn remains separate from this history: interactive submission
loads history before reserving the new rows, queued dispatch excludes both
reserved current-turn IDs, and compressor requests keep
`projected_user_message=None` while the classifier bundle retains the active
text only in `current_user_turn`.
Snapshot insertion is the final idempotency authority. The epoch includes a
deterministic digest of the exact backend source-ID sidecar, so different
histories with the same estimated token count do not collide. Persistence
locks the task row, resolves existing summary metadata inside the same
transaction, and returns an exact epoch/cutoff match without writing another
row. Every validated candidate reaches this locked authority; there is no
token-count-only precheck. A repeated epoch with a different cutoff is rejected
before reservation, and any partial reservation/update failure rolls back.
The initial, interrupt-resume, and checkpoint-retry success finalizers do not
recompress or persist context. The validated pre-classifier operation is the
only compressor and summary-write authority for a user turn.
Because the persisted cutoff is the final source ID in the exact expired
prefix used to build the validated classifier candidate, a fresh reader session
reconstructs the same summary and retained complete-turn tail, with the same
backend source-ID alignment, after refresh or process restart.
The existing status stream also exposes an awaited `status/context_window`
lifecycle emitter for compaction. It accepts only `compacting`, `completed`,
`failed`, or `cancelled`, carries task/conversation/turn/epoch identity, and
delegates to the shared awaited hub publisher so callers can order subsequent
work after stream sequence assignment. The facade passes its existing turn
identity into the pre-classifier compression service; that service awaits the
matching `compacting` packet before its first compressor call and spans all
whole-turn candidate attempts plus snapshot persistence before awaiting exactly
one matching terminal packet. Success emits `completed`, provider/validation/
persistence failures emit `failed`, and task cancellation emits `cancelled`.
While that scope is active, its owning asyncio task is registered under the
same task/turn identity. An accepted `RunLifecycleService.request_cancel`
cancels only that registered owner, so in-flight provider work receives
`CancelledError`, the matching cancelled lifecycle is awaited, and snapshot
persistence cannot run afterward.
The multi-task stream manager writes these lifecycle packets into the existing
task/conversation-keyed context-window store before broadcasting its legacy
compatibility event. The store applies the turn/epoch/sequence gate reducer,
the context-window hook exposes the resulting state, and only the matching task
composer disables submission and shows the inline compaction status. The
composer draft remains controlled state and is not cleared when the gate opens
or closes. If the multiplex stream disconnects, including its direct
`open`-to-reconnecting transition, the manager releases active gates only for
its desired task IDs while retaining lifecycle identity and the last sequence;
stale replay therefore cannot re-gate the composer after recovery.

## Shared Graph State

The main graph state is `InteractiveState` from `agent/graph/state.py`.

Core partitions:

- `facts`
  - user message, conversation id, capability, metadata, plan/todos, selected
    tool state, decision history, budget counters.
- `trace`
  - reasoning, observations, executed tools, history, final text, usage records.

State is passed through LangGraph as dictionaries, with wrappers in
`agent/graph/builders/common_edges.py` converting to typed state where needed
and injecting runtime context/config/writer only for nodes that accept them.

## Normal Chat Graph

Builder: `agent/graph/graph_builder.py::build_simple_chat_graph`

```mermaid
flowchart LR
    classification --> update_working_memory
    update_working_memory --> memory_retrieval
    memory_retrieval --> simple_chat
    simple_chat --> post_process
    post_process --> finalize
    finalize --> END
```

Purpose:

- respond without tool execution
- use shared context bundle and working-memory update path
- stream/finalize an assistant message through the normal completion callback

## Simple Tool Graph

Builder: `agent/graph/builders/simple_tool_builder.py::build_simple_tool_graph`

```mermaid
flowchart TD
    classification --> Gate{simple_tool_execution?}
    Gate -- no --> finalize
    Gate -- yes --> update_working_memory
    update_working_memory --> memory_retrieval
    memory_retrieval --> select_tool_categories
    select_tool_categories --> prepare_tool_plan
    prepare_tool_plan --> PrepRoute{route}
    PrepRoute --> articulation
    PrepRoute --> approval_gate
    PrepRoute --> post_tool_reasoning
    articulation --> approval_gate
    approval_gate --> dispatch_tool
    dispatch_tool --> tool_synthesizer
    tool_synthesizer --> post_tool_reasoning
    post_tool_reasoning --> decision_router
    decision_router --> select_tool_categories
    decision_router --> think_more
    decision_router --> reflect
    decision_router --> synthesis
    decision_router --> format_results
    think_more --> post_tool_reasoning
    reflect --> decision_router
    synthesis --> format_results
    format_results --> finalize
    finalize --> END
```

Important boundaries:

- Capability gate exits to `finalize` when simple-tool execution is unavailable.
- `prepare_tool_plan` can bypass execution and send unavailable-capability state
  to post-tool reasoning.
- `articulation` runs only for the first attempt; retries go directly to the
  approval gate.
- `approval_gate` interrupts when approval is required.
- `dispatch_tool` executes only after approval.
- `post_tool_reasoning` emits candidate decisions; `decision_router` is the
  deterministic route authority.

## Deep Reasoning Graph

Builder: `agent/graph/builders/deep_reasoning_builder.py::build_deep_reasoning_graph`

```mermaid
flowchart TD
    classification --> Gate{deep_reasoning?}
    Gate -- no --> fallback_finalize
    Gate -- yes --> update_working_memory
    update_working_memory --> memory_retrieval
    memory_retrieval --> clarify_gate
    clarify_gate --> planner
    clarify_gate --> finalize
    planner --> plan_review
    planner --> clarify_gate
    plan_review --> handle_unavailable_tools
    plan_review --> decision_router
    plan_review --> finalize
    handle_unavailable_tools --> planner
    handle_unavailable_tools --> finalize
    decision_router --> select_categories
    decision_router --> think_more
    decision_router --> reflect
    decision_router --> synthesis
    decision_router --> finalize
    think_more --> post_tool_reasoning
    select_categories --> prepare_tool_plan
    prepare_tool_plan --> approval_gate
    prepare_tool_plan --> post_tool_reasoning
    approval_gate --> dispatch_tool
    dispatch_tool --> tool_synthesizer
    tool_synthesizer --> post_tool_reasoning
    post_tool_reasoning --> observation_adapter
    observation_adapter --> decision_router
    reflect --> decision_router
    synthesis --> finalize
    finalize --> fallback_finalize
    fallback_finalize --> END
```

Important boundaries:

- Deep reasoning adds `clarify_gate`, `planner`, `plan_review`, and
  `handle_unavailable_tools`.
- Plan review can interrupt for human approval when HITL interrupts are enabled.
- Tool approval still keys off autonomy mode, not plan routing alone.
- Tool execution uses the same shared approval/dispatch/synthesizer/PTR path as
  simple-tool.
- `observation_adapter` converts post-tool findings into compact observations
  before returning to `decision_router`.
- PTR obtains optional visible narration and exactly one internal `ptr_commit`
  from the same provider-neutral model turn. Provider adapters stream only
  assistant text, buffer tool arguments until completion, and expose terminal
  truncation state. A missing or truncated commit triggers one commit-only
  recovery call over existing evidence; completed external work is not rerun.
- Terminal path is `finalize -> fallback_finalize -> END`.

## Subagent Child Graph

Builder: `agent/subagents/runtime/graph.py::build_subagent_graph`

```mermaid
flowchart TD
    initialize --> model
    model --> approval_gate
    model --> handoff
    approval_gate --> dispatch_tool
    dispatch_tool --> tool_synthesizer
    tool_synthesizer --> observation
    observation --> model
    handoff --> END
```

Important boundaries:

- `initialize` normalizes declarative definition metadata, assignment identity,
  graph thread identity, and the definition-owned tool profile.
- `model` builds the versioned `subagent_runtime` prompt, binds all
  definition-visible native tools with runtime scheduling metadata, and routes
  either to shared tool execution or directly to handoff text.
- `approval_gate`, `dispatch_tool`, and `tool_synthesizer` reuse the existing
  shared tool execution subgraph.
- `observation` syncs the completed tool-iteration budget from canonical
  `working_memory.current_turn_phases` records before returning to the same
  model session.
- `handoff` derives the terminal state from canonical working memory, compact
  tool results, executed-tool records, and model handoff text, then projects it
  into the generic `AgentResult` contract. Child PTR, decision-router,
  think-more, reflect, and separate child-finalizer nodes are not part of the
  subagent graph.

## Parent Handoff Continuation Graph

Builder: `agent/graph/builders/parent_handoff_builder.py::build_parent_handoff_graph`

```mermaid
flowchart TD
    prepare_handoff_context --> post_action_reasoning
    post_action_reasoning --> decision_router
    decision_router --> select_tool_categories
    decision_router --> think_more
    decision_router --> reflect
    decision_router --> synthesis
    decision_router --> format_results
    decision_router --> delegate_subagent
    decision_router --> wait_for_subagents
    select_tool_categories --> prepare_tool_plan
    prepare_tool_plan --> articulation
    prepare_tool_plan --> approval_gate
    prepare_tool_plan --> prepare_direct_tool_context
    articulation --> approval_gate
    approval_gate --> dispatch_tool
    dispatch_tool --> tool_synthesizer
    tool_synthesizer --> prepare_direct_tool_context
    prepare_direct_tool_context --> post_action_reasoning
    think_more --> post_action_reasoning
    reflect --> decision_router
    synthesis --> format_results
    format_results --> finalize
    finalize --> END
    delegate_subagent --> END
    wait_for_subagents --> END
```

Important boundaries:

- The coordinator calls this graph only after
  `ProcessLocalAgentRunRegistry.claim_ready_handoffs` returns a claimed
  terminal-result batch for the same tenant, task, and conversation.
- `prepare_handoff_context` marks the first reasoning input as
  `post_action_outcome_source="subagent_handoff_batch"` and removes direct-tool
  synthesized output from metadata before entering the existing
  `post_tool_reasoning` node under its post-action role.
- If parent PAR chooses a direct tool, the graph reuses the same
  `select_tool_categories -> prepare_tool_plan -> approval_gate ->
  dispatch_tool -> tool_synthesizer` path as the simple-tool graph, then marks
  the next reasoning input as `post_action_outcome_source="direct_tool"`.
- `delegate_subagent` and `wait_for_subagents` are backend-owned coordination
  outcomes. The graph records the router outcome and stops at `END`; the
  coordinator validates follow-up handoffs through the shared ownership policy
  and assignment builder, or waits on the registry for the next ready handoff
  or inactive state.
- `decision_router` remains the deterministic route authority. It rejects
  malformed `delegate_subagent`, rejects `wait_for_subagents` without relevant
  active runs, and converts finalization into an explicit wait when relevant
  active child runs remain.

Coordination source of truth:

- `backend/services/agent_runs/registry.py` owns process-local run lifecycle,
  ready-result claims, acknowledgement, release, and active-run snapshots.
- `backend/services/agent_runs/parent_handoff_coordinator.py` serializes one
  parent continuation per tenant/task key, including parent cycles from
  different turns of the same task. A claimed batch is
  acknowledged only after parent state processing succeeds; errors and
  cancellations release the claim for retry.
- Runtime side effects remain in graph tool execution and runtime-provider/tool
  boundaries. Backend coordination never injects service objects, DB sessions,
  SDK clients, or decrypted secrets into graph state.

## Shared Tool Execution Subgraph

The shared tool execution path lives in `agent/graph/subgraphs/tool_execution.py`
and delegates runtime details under
`agent/graph/subgraphs/tool_execution_runtime/*`.

Shared sequence:

```text
select categories -> prepare_tool_plan -> approval_gate -> dispatch_tool
  -> tool_synthesizer -> post_tool_reasoning
```

Runtime responsibilities under the subgraph:

- build request context from graph metadata
- prepare workspace files/directories
- validate and execute tool batches
- apply approval and idempotency behavior
- dispatch local, direct, or runner-backed commands
- project compact result metadata back into graph state
- record artifact/provenance metadata

## Streaming And Checkpointing

`LangGraphExecutor.stream_graph` runs compiled graphs using both stream modes:

- `custom`
  - node-authored stream events for reasoning, tool, answer, pause, and status
    updates
- `values`
  - full graph state snapshots for checkpointing, final-state capture, and
    interrupt detection

The executor forwards processed custom events to the in-memory stream hub. The
hub persists replayable stream packets separately from graph checkpoints.
At each `values` checkpoint the executor may still evaluate the context window
and emit the existing ceiling status/handoff metadata. This observation is
read-only: it does not call the context compressor, call `aupdate_state`, or
replace legacy `facts.metadata["conversation_history"]`. The validated
pre-classifier path described above remains the only compaction authority.

Interrupt handling:

- interrupt payloads are observed from `values` events
- stream execution continues long enough for checkpoint state to persist
- handlers return interrupted results without final assistant text
- resume paths rebuild the graph from checkpoint/thread identity and continue
  from the interrupt point

Provider refusals are a separate terminal public outcome. The workflow remains
internally `FAILED`, while workflow metadata records
`outcome_type="provider_refusal"`; live and reconstructed assistant metadata use
`status="declined"` and `stop_reason="refusal"`. Public run status, run-state
events, and retry lifecycle packets use the same terminal `declined` state;
refusal rows do not carry chat error codes, and provider explanations are
stored only through the bounded plain-text refusal projection.

## Runtime Context

Graph wrappers extract `GraphRuntimeContext` from state metadata and forward
runtime context/config/writer only to node signatures that accept them.

Runtime metadata includes:

- tenant/task/user identity
- graph thread and turn identity
- runtime placement and workspace identity
- provider/model/reasoning metadata
- credential refs, not plaintext credentials
- conversation context bundle
- environment/runtime projection when available

Live runtime service objects are attached to LangGraph config at invocation time
and stripped before checkpoint/state inspection or diagnostics.

## Security And Isolation Notes

- Graph state must remain serializable and must not carry decrypted provider
  secrets, SDK clients, DB sessions, or backend service objects.
- Tool execution nodes rely on backend-provided task/runtime identity, not model
  supplied authority fields.
- Approval gates interrupt before dispatch; dispatch nodes do not re-plan on
  resume.
- Capability gates fail into finalize paths instead of executing unavailable
  graph capabilities.
- Context bundle and projections are prompt authority; UI transcript query
  services are read models, not prompt assembly sources.

## Operational Notes

- Graph names are centralized in `agent/graph/graph_names.py`.
- Normal chat graph compiles in the normal-chat handler.
- Simple-tool graph compiles in the simple-tool handler and can also be
  retrieved through the graph registry helper.
- Deep-reasoning active handler path compiles with a per-task checkpointer.
- Deterministic scenario graphs can replace live builders in deterministic test
  mode.
