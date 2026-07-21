# Execution Plane Architecture

Code-verified overview of task runtime execution, runtime provider selection,
LangGraph orchestration, product Runner dispatch, and explicit dev/test local
Docker provider behavior.

## Detail Docs

- [Agent Architecture](agent-architecture.md)
- [LangGraph Graph Architecture](langgraph-graph-architecture.md)
- [Tool Architecture](tools.md)
- [Model Architecture](models.md)
- [Runtime Provider Architecture](runtime-provider.md)
- [Workspace And Artifact Architecture](workspace-artifacts.md)
- [Artifact Provenance Architecture](artifact-provenance.md)

## Purpose

The execution plane performs task work. It runs LangGraph turns, executes tools,
opens terminals, reads/writes runtime artifacts, and reports runtime results
back to the management and data planes.

It does not own tenant membership, user permissions, or cross-task data access.
Those decisions are resolved before runtime operations are dispatched.

## Responsibility Boundary

Owned by the execution plane:

- LangGraph branch execution for chat, direct executor, and deep reasoning.
- Runtime provider operations for product Runner placement and explicit
  dev/test/diagnostic local placement.
- Tool command dispatch and result collection.
- Runner-owned Kali container lifecycle and `/workspace` execution model.
- Runner lifecycle, terminal, artifact, metadata, and tool-command operations.
- Runtime observations, artifacts, logs, metrics, terminal output, and VPN state.

### Managed task networking

New and recreated task runtimes receive one non-internal user-defined Docker
bridge named from the existing container identity with a `-net` suffix. The
local Docker and Runner providers share the same backend-free contract for
ownership labels and deterministic collision-safe `/29` allocation. The
default pool is `198.18.0.0/15`; operators may override it with
`DROWAI_RUNTIME_NETWORK_POOL`, which must be a non-global IPv4 CIDR capable of
providing a `/29`. Invalid or exhausted pools fail runtime provisioning rather
than falling back to Docker's legacy bridge.

The bridge enables masquerading for internet/VPN control connectivity and
disables inter-container communication. Local runtimes retain backend access
through Docker's `host-gateway` mapping for `host.docker.internal`; Runner
runtimes do not gain a runner-host mapping. Permanent runtime retirement removes
only an empty network carrying the expected DrowAI ownership labels. Stop and
pause operations leave it intact, and existing containers are not reattached.

OpenVPN may install more-specific routes through `tun0`, so VPN targets and
network scanners continue to use the tunnel while ordinary traffic uses the
task bridge. Operators should select a non-overlapping override pool if a VPN
also routes the default managed range.

Not owned by the execution plane:

- HTTP route authorization.
- Tenant membership selection.
- Durable report/knowledge policy decisions.
- Secret storage.
- Frontend cache or routing behavior.

## Wired Entrypoints

- `backend/routers/tasks/router_bundle.py`
  - Composes task CRUD, runtime, interrupt, inbox, file, scope, log, metric,
    container, and VPN routes for the `/api/tasks` surface.
- `backend/routers/tasks/crud.py`
  - Task creation route; delegates bootstrap orchestration to
    `TaskLifecycleService`.
- `backend/routers/tasks/interrupts.py` and
  `backend/routers/tasks/interrupt_inbox.py`
  - Human-in-the-loop interrupt inspection, resume, retry, and inbox routes.
- `backend/services/langgraph_chat/facade.py`
  - Chat turn orchestration and branch selection.
- `backend/services/langgraph_chat/execution/graph_executor.py`
  - LangGraph execution and stream adaptation.
- `backend/services/task/lifecycle_service.py`
  - Task creation, admission, workspace materialization, queueing, provider
    provisioning, and startup finalization.
- `backend/services/task/admission_service.py`
  - User/tenant quota gates and runner-capacity admission for task creation.
- `backend/services/task/interrupt_service.py` and
  `backend/services/task/graph_retry_service.py`
  - Authoritative interrupt ticket resume and checkpoint retry orchestration.
- `backend/services/runtime_provider/registry.py`
  - Placement-to-provider resolution.
- `backend/services/runtime_provider/product_policy.py`
  - Product placement policy that rejects Management-owned local Docker for
    product task paths.
- `backend/services/runtime_provider/contracts.py`
  - Runtime operation request/result envelope.
- `backend/services/runtime_provider/local_docker_provider.py`
  - Explicit dev/test/diagnostic Local Docker-backed provider.
- `backend/services/runtime_provider/cloud_runner_provider.py`
  - Managed runner-backed provider.
- `backend/services/docker/*`
  - Docker client, config, lifecycle, logs, metrics, exec, and operations.
- `kali_executor/executor_daemon.py`
  - In-container file-comm executor daemon.
- `agent/communication/file_comm.py` and
  `kali_executor/communication/file_comm.py`
  - Host/container JSONL command/result transport.

## Runtime Placement

Product task runtime placement is resolved before provider dispatch:

- Product tasks use `runner` -> `CloudRunnerRuntimeProvider`.
- Explicit dev/test/diagnostic callers may use `local` ->
  `LocalDockerRuntimeProvider`.

Provider requests include:

- `tenant_id`
- `task_id`
- `actor_type` and `actor_id`
- `user_id`
- `runtime_placement_mode`
- `workspace_id`
- `runner_id`
- `execution_site_id`
- operation name, payload, and metadata

Unsupported placement modes fail closed in the registry.

## Task Creation And Admission Flow

Task creation enters through the task CRUD router and immediately crosses into
`TaskLifecycleService`; the router owns HTTP authorization and response
mapping, not runtime bootstrap decisions.

```mermaid
flowchart TD
    Route[POST /api/tasks]
    Lifecycle[TaskLifecycleService.create_task]
    Policy[Product runtime placement policy]
    Admission[AdmissionControlService]
    TaskRow[Task row + runner/workspace identity]
    Workspace[materialize_runtime_workspace]
    Queue[TaskStateService: created -> queued -> starting]
    Provider[RuntimeOperationService -> runtime provider]

    Route --> Lifecycle
    Lifecycle --> Policy
    Lifecycle --> Admission
    Admission --> TaskRow
    Lifecycle --> Workspace
    Lifecycle --> Queue
    Queue --> Provider
```

The lifecycle service resolves engagement and product runtime placement before
admission. `AdmissionControlService` owns Gate A user/tenant quota checks and
Gate B physical capacity: runner placement selects an eligible runner and
execution site, while explicit local placement may apply a deployment-wide
active-task ceiling. Admission wraps the first counted task write in the same
transaction boundary as the capacity checks.

After admission succeeds, the task row carries durable tenant/user identity,
`runtime_placement_mode`, optional `runner_id` and `execution_site_id`, and a
stable `workspace_id` defaulting to `task-<id>`. Workspace materialization is
provider-owned. For runner placement it is a management-plane no-op carrying
workspace identity; for explicit local placement it prepares the host workspace
layout. The service then queues the task, starts background initialization,
moves the task to `starting`, and dispatches `provision_task_runtime` through
`RuntimeOperationService` so provider requests include the canonical task,
tenant, actor, workspace, runner, and execution-site envelope.

Runner placement provisioning is asynchronous. An accepted runner operation
leaves the task in `starting` until the authenticated runner publishes a
lifecycle runtime event. Explicit local placement can complete startup
synchronously and move the task to `running` after provider success.

## Explicit Local Docker Runtime

Local runtime work is delegated through `LocalDockerRuntimeProvider` to the
Docker service facade and workspace manager. This is not a product execution
path for standalone or distributed deployments and must not be used as a
Management-host fallback when no Runner is connected.

```mermaid
flowchart LR
    Provider[LocalDockerRuntimeProvider]
    Docker[Docker service facade]
    HostWorkspace[agent/workspaces/task-id data]
    HostControl[agent/runtime-control/task-id control]
    Container[Kali container]
    Daemon[executor_daemon.py]

    Provider --> Docker
    Docker --> HostWorkspace
    Docker --> HostControl
    Docker --> Container
    HostWorkspace --> Container
    Container --> Daemon
    Provider -->|cancellations.jsonl| HostWorkspace
    Daemon -->|ack consumed cancellations| HostWorkspace
```

Key boundaries:

- Host task workspace is mounted as `/workspace`.
- Host control material is mounted read-only as `/run/drowai/control`; VPN and
  runtime input are not workspace-visible artifacts.
- Command transport uses lock-protected `commands.jsonl`, `results.jsonl`, and
  `cancellations.jsonl`.
- `LocalDockerRuntimeProvider.cancel_tool_command` owns local cancellation
  dispatch by appending cancellation rows for file-comm command ids; unsupported
  command transports are reported as not kill-supported.
- The executor daemon polls cancellation rows while each prepared subprocess is
  active, terminates the subprocess process group with SIGTERM then SIGKILL if
  needed, records `user_cancelled` result metadata, and acknowledges consumed
  cancellations by removing their rows.
- The executor daemon runs prepared command envelopes under `/workspace`.
- Docker implementation details live under `backend/services/docker/*`.
- Runtime images must report workspace layout `2.0`; mismatches stop startup.
- `backend/services/unified_docker_service.py` preserves compatibility imports.

## Product Runner Runtime

Managed runner work is delegated through `CloudRunnerRuntimeProvider` to
runner-control jobs and messages.

```mermaid
sequenceDiagram
    participant Provider as CloudRunnerRuntimeProvider
    participant Jobs as RuntimeJobService
    participant Messages as RunnerControlMessage
    participant Runner as Connected runner
    participant Events as RuntimeEventService
    participant State as TaskStateService

    Provider->>Jobs: create/assign runtime job
    Provider->>Messages: persist outbound command
    Provider-->>Provider: return accepted/pending operation
    Runner->>Messages: receive/ack command
    Runner->>Events: publish lifecycle/result event
    Events->>Jobs: transition runtime job
    Events->>State: apply task state for lifecycle event
```

Key boundaries:

- Runner placement requires managed runner control to be enabled.
- Durable rows store runtime job and message state.
- Runner connections and leases are tenant-bound.
- Provider lifecycle calls such as start, pause, resume, stop, and retire create
  runtime jobs and outbound messages, then normally return an accepted pending
  result.
- `RuntimeEventService` is the backend authority that maps `runtime.started`,
  `runtime.paused`, `runtime.resumed`, `runtime.stopped`, `runtime.retired`,
  and lifecycle `runtime.failed` events onto task state through
  `TaskStateService`.
- Raw reusable-secret command durability is intentionally limited; durable
  control rows are masked.
- Standalone Compose starts Management and Runner together from
  `deploy/compose/standalone.yml`; distributed Management uses
  `deploy/cloud/control-plane.yml`; Runner hosts use
  `deploy/cloud/execution-site-package/compose.yml`.
- Runner Site removal only removes idle execution capacity. Live execution blocks
  removal, and the final connected authorized Runner cannot be removed through
  the normal operation. Runner removal never owns or changes parent task state.

## Interrupt, Resume, And Retry Flow

Human-in-the-loop state is task-bound and ticket-authoritative. The interrupt
inbox route lists pending `InterruptTicket` rows scoped to the active tenant and
current task owner. The per-task interrupt lookup route authorizes the task,
loads the latest pending ticket, and hydrates checkpoint state only when the
stored ticket identity matches the live interrupt snapshot.

Resume posts claim the canonical pending ticket through
`InterruptTicketService.claim_for_resume`; a missing or no-longer-pending ticket
returns a typed HTTP error instead of reusing stale client state. After claim,
`TaskInterruptService` reconciles checkpoint identity from the live interrupt
snapshot, begins the durable resume workflow through `TurnWorkflowService`, and
enqueues `run_resume_generation` with the resolved graph thread, tenant,
workspace, runtime placement, runner identity, checkpoint, resume key, and
interrupt id. If enqueueing fails, the service performs the only allowed
requeue path by returning the workflow and ticket to a pending human-waiting
state.

Checkpoint retry is separate from interrupt resume. The retry route delegates to
`TaskGraphRetryService`, which validates task ownership, asks
`TurnWorkflowService` to atomically claim the failed turn for checkpoint retry,
returns idempotent identity for an already-running retry, and enqueues
`run_checkpoint_retry_generation` with sanitized prior-failure metadata and the
canonical retry identity.

## LangGraph Runtime

LangGraph execution is backend-orchestrated but runtime-facing.

Flow:

1. Chat route reserves chat messages and enters backend generation.
2. Background generation calls `run_langgraph_generation`.
3. `LangGraphChatFacade` resolves deployment-bound text-LLM selection before
   graph execution and attaches only non-secret runtime metadata plus live
   runtime services.
4. Graph nodes call runtime/tool services through configured runtime context.
5. The LLM runtime resolver revalidates the deployment reference, owner,
   revision, route, and credential before constructing a provider client.
6. Stream events publish live packets and persist replay rows.
7. Completion callbacks finalize chat messages, tool rows, usage, and state.

Branches:

- Normal chat: response-only graph.
- Direct executor: bounded progressive tool execution.
- Deep reasoning: plan-oriented graph path.

## Security And Isolation Notes

- Runtime operations must be task-bound and tenant-bound through provider
  envelopes.
- Runtime file access should remain inside the task workspace.
- Runtime providers should return normalized operation results rather than
  leaking provider-specific internals to routers.
- Decrypted LLM credentials should not be serialized into graph state,
  checkpoints, stream packets, runner messages, or logs.
- Tool execution in task runtimes should not bypass scope validation and
  workspace-safe path helpers.

## Operational Notes

- Product containers are created by Runner using the configured runtime image
  contract and `/workspace` mount policy.
- Runner placement is required for product standalone and distributed
  deployments.
- Terminal channels use runtime provider operations for runner placement and
  Docker exec PTY behavior only for explicit local placement.
- VPN state is task-specific. Local placement materializes and connects after
  provisioning succeeds; Runner placement waits for the accepted
  `runtime.started` event before dispatching VPN config and reconnect operations.
- Configure/upload requests made before the task reaches `running` persist the
  VPN as `configured` without entering the runtime. Status reads return that
  persisted state and Retry returns a conflict until runtime execution is safe.
  Successful provisioning commits `running` before the serialized VPN
  materialize/reconnect sequence, preventing configuration updates from racing
  the startup handoff.
- VPN connection failure does not fail the task runtime. The provider persists
  VPN state separately, and the existing runtime-log stream merges bounded,
  sanitized `/vpn/connection.log` entries into Docker Terminal rows tagged with
  `service: vpn`. VPN tail probing is best-effort, so an exited container or a
  rejected Docker exec does not suppress already available container logs.
