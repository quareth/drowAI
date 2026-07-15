# DrowAI Code Compass

This is a placement map for contributors. Use it to answer two questions:

1. Where does an existing responsibility live?
2. Where should new code for that responsibility go?

It is intentionally an index, not a narrative architecture document. The map
was verified against tracked source, imports, router composition, process
entrypoints, package manifests, and runtime-image copy rules. Re-check the wired
code before relying on a row after structural changes.

In this document, **owner** means the module that owns a responsibility. It does
not mean a person or GitHub team.

## Fast Direction

| If you are adding or changing... | Go to... | Keep out of... |
|---|---|---|
| An HTTP or WebSocket endpoint | `backend/routers/` | ORM models and graph nodes |
| A backend use case or workflow | The capability package under `backend/services/` | Routers |
| A pure task rule or lifecycle invariant | `backend/domain/` | FastAPI and database code |
| An API request/response contract | `backend/schemas/` | ORM models |
| A database table or relationship | `backend/models/` plus `backend/migrations/` | Schemas and routers |
| Reusable query/persistence behavior | The owning service; use `backend/repositories/` for a deliberately shared repository boundary | Routers |
| Environment parsing or deployment policy | `backend/config/` or `deploy/env_contract.py` | Feature services |
| A runtime side effect for a task | `backend/services/runtime_provider/` | Direct Docker/runner calls from routers or graph nodes |
| Local Docker provider behavior | `backend/services/docker/` and `backend/services/runtime_provider/local_docker_provider.py` | Provider-neutral services |
| Managed-runner control-plane behavior | `backend/services/runner_control/` or `backend/services/runtime_provider/cloud_runner/` | `drowai_runner/` unless it must execute at the site |
| Managed-runner site behavior | `drowai_runner/` | Backend packages |
| A backend/runner/runtime shared wire or safety contract | `runtime_shared/` | Framework-specific packages |
| In-container command execution | `kali_executor/` | Backend authorization and placement policy |
| LangGraph state or graph behavior | `agent/graph/` | Backend routers |
| Tool planning, admission, or transport selection | `agent/tool_runtime/` | Individual tool implementations |
| A tool definition or tool-specific parser | The matching category under `agent/tools/` | Graph nodes and transport routing |
| A prompt or prompt builder | `core/prompts/` | Inline strings at call sites |
| Shared LLM role/model policy | `core/llm/` | Provider UI and individual graph nodes |
| LLM adapter implementation/profile | `agent/providers/llm/` | Backend credential persistence |
| LLM credentials, selection, or runtime secret resolution | `backend/services/llm_provider/` | Graph state and frontend |
| A route-level frontend screen | `client/src/pages/` and `client/src/App.tsx` | Generic UI primitives |
| A self-contained frontend capability | `client/src/features/<capability>/` | Global hooks unless truly cross-feature |
| Non-React frontend transport logic | `client/src/services/` | Components |
| Task-scoped frontend client state | `client/src/state/` | Transport clients |
| A reusable visual primitive | `client/src/components/ui/` | Backend/API behavior |
| A focused test | Beside the owning subsystem's test tree | Unrelated root test folders |
| Cross-process or release certification | `tests/`, `e2e/`, or `scripts/run_release_gate.py` | Product runtime modules |

## Dependency and Ownership Boundaries

These are placement rules reflected in the active code paths.

- `client/` reaches the application through HTTP and WebSocket contracts. It
  does not import Python implementation modules.
- Backend routers authenticate, authorize, validate transport input, and call
  services. Business orchestration belongs in services even when an existing
  compatibility route is thicker than desired.
- ORM modules declare tables and relationships. They do not own workflows,
  authorization, or response serialization.
- `RuntimeOperationService` is the intended management-plane boundary for task
  runtime side effects and resolves a `RuntimeProviderRegistry` provider from
  task runtime identity. It is not universal current wiring: the active graph
  tool runner lane constructs `RuntimeOperationRequest` and resolves/invokes a
  provider directly, while its local lane uses the agent executor/tool-runtime
  transport path. Treat both graph dispatch paths as existing boundary drift,
  not as patterns for new management-plane operations.
- Local provider internals stay in `backend/services/docker/`; managed provider
  dispatch stays in `backend/services/runtime_provider/cloud_runner/` and
  `backend/services/runner_control/`.
- `drowai_runner/`, `kali_executor/`, and `runtime_shared/` do not import
  backend application code in their production modules. Preserve that process
  boundary.
- `runtime_shared/` contains backend-free DTOs, normalization, workspace,
  protocol, network, terminal, and masking primitives, plus the shared Docker
  SDK task-network adapter in `docker_network_manager.py`. Keep provider SDKs
  out of its pure contract modules, and keep the package free of FastAPI,
  SQLAlchemy, and backend application-service dependencies.
- Graph/checkpoint state contains serializable data and runtime identity, not
  database sessions, provider clients, service instances, or decrypted secrets.
- Task filesystem access uses the existing workspace resolvers and
  `WorkspaceFilesystem`; never introduce arbitrary host-path access.
- Package `__init__.py` files expose stable or lazy public surfaces. Put
  behavior in a focused module, not in the aggregate export file.
- A helper belongs beside its owner until at least two independent owners need
  it. Do not create a generic `utils` destination for single-use behavior.

Current code has some dependency exceptions: parts of `agent/graph/` import
backend application services, configuration, persistence/session helpers,
metrics, workspace, and runtime contracts, and some `core/prompts/` builders
import agent graph helpers. Treat those as existing coupling, not permission to
add new cross-boundary imports.

## Wired Process Entrypoints

| Process or surface | Wired entrypoint | Owner |
|---|---|---|
| Canonical developer workflow and managed-runner parity stack | `python3 scripts/local_production_cloud.py up` | Product-canonical local development command; starts the backend, managed runner cloud client, and frontend as separate processes |
| Backend application | `backend/main.py` | FastAPI lifespan, mounted routers, `/ws` channel multiplexer |
| Frontend application | `client/src/main.tsx` -> `client/src/App.tsx` | React providers, setup gate, top-level routes, runtime-stream bootstrap |
| Managed runner CLI | `drowai_runner.app:main` | `drowai-runner` commands and cloud-mode startup |
| Runner cloud connection | `drowai_runner/control_channel/entrypoint.py` | Builds and runs the outbound runner client |
| Kali runtime daemon | `kali_executor/executor_daemon.py` | Runtime metadata probe and JSONL command execution loop |
| Runtime image | `runtime/image/Dockerfile` | Copies the executor and the allowed `runtime_shared` subset into `/opt/drowai/runtime` |
| Release test gate | `scripts/run_release_gate.py` | Curated release-blocking test orchestration |

## Repository Root Map

| Path | Owner / purpose | Placement note |
|---|---|---|
| `backend/` | FastAPI control plane, persistence, application services, runtime dispatch | Backend product behavior starts here |
| `agent/` | Agent contracts, LangGraph graphs, planning, tools, and tool runtime | Does not own SaaS authorization or runner registration |
| `client/` | React/TypeScript product UI | Browser-only behavior and public API contracts |
| `core/` | Canonical prompts, LLM role policy, runbooks, and tool taxonomies shared by backend/agent call sites | Not a general dumping ground; some current modules are not dependency-free |
| `drowai_runner/` | Managed execution-site process | Backend-free runner implementation |
| `kali_executor/` | Daemon copied into the Kali task runtime | Executes prepared commands; does not decide authorization or placement |
| `runtime_shared/` | Backend-free contracts and the shared Docker task-network adapter used across processes | Lowest backend-free runtime boundary; pure contract modules remain provider-SDK-free |
| `runtime/` | Runtime image, manifest, and VPN assets | Packaging/runtime assets, not application orchestration |
| `deploy/` | Compose/cloud profiles, images, installers, and environment contract | Deployment ownership |
| `scripts/` | Build, package, seed, local-parity, regression, and release commands | Operator/developer entrypoints, not importable domain logic |
| `e2e/` | Playwright fixtures, journeys, probes, and secret-safe reporting | Browser and runtime certification |
| `tests/` | Cross-package, runner, runtime-image, script, integration, and compatibility tests | Use when ownership spans packages |
| `docs/` | Architecture, runbooks, testing, and tooling documentation | Documentation is descriptive, never runtime authority |
| `.github/` | CI workflows | Automation wiring |
| `.codex/`, `.cursor/` | Contributor-agent configuration and workflows | Development support; not application runtime |
| Root `Dockerfile.*`, `vite.config.ts`, `tsconfig.json`, Tailwind/PostCSS config | Build surfaces | Change only for build/runtime packaging needs |

## Backend Map

### Backend layers

| Path | Owner | New functions placed here when... |
|---|---|---|
| `backend/main.py` | Application composition | Mounting a router, lifecycle service, middleware, or top-level WS channel; not for feature logic |
| `backend/auth.py` | JWT/password primitives and auth dependencies | Changing token issuance/validation or authenticated-user resolution |
| `backend/database.py` | Engine, session, `Base`, shared DB primitives, schema-readiness checks | Changing DB bootstrap/session mechanics, not feature queries |
| `backend/config/` | Typed environment/configuration policy | Multiple callers require the same validated setting |
| `backend/core/` | Backend-wide logging, rate, network, and time primitives | The helper is truly backend-wide and framework/service neutral |
| `backend/domain/` | Pure task lifecycle and task-admission rules | The rule needs no HTTP, ORM session, Docker, or provider client |
| `backend/models/` | SQLAlchemy table/relationship definitions | Persisted shape changes; add a migration too |
| `backend/models/__init__.py` | Aggregate ORM import/export surface | Re-exporting a model for metadata/import compatibility; no model behavior |
| `backend/schemas/` | Pydantic HTTP contracts | Public request/response shape changes |
| `backend/repositories/` | Focused provenance and reporting repositories | Persistence behavior is intentionally reusable and transaction control stays with callers |
| `backend/repositories/reporting/` | Report, job, worker, memo, and retention persistence | Keep requester, worker, artifact, and retention query ownership separated |
| `backend/migrations/` | Alembic migration runtime and revisions | Database schema changes |
| `backend/routers/` | HTTP/WS adapters | Translating a request into an authorized service call |
| `backend/services/` | Application capability owners | Coordinating domain, persistence, provider, and event behavior |
| `backend/scripts/` | Backend-specific backfills, validators, seed and type generation | One-off or operator-invoked data maintenance |
| `backend/utils/` | Small backend-wide utilities | Only when no capability owner exists; currently ANSI cleanup only |
| `backend/tests/` | Backend-focused tests | The behavior is owned by backend code |

### Configuration, storage, and wire modules

| Path | Owner |
|---|---|
| `backend/config/container_config.py` | Local container settings and resource configuration |
| `backend/config/data_plane.py` | Object-store/data-plane settings and validation |
| `backend/config/deployment_topology.py` | Deployment profile resolution and fail-closed topology validation |
| `backend/config/feature_flags.py` | Typed backend feature/runtime flags |
| `backend/config/generated_config.py` | Generated deployment configuration and secret-file handling |
| `backend/config/reporting.py` | Reporting safety/runtime settings |
| `backend/config/retention.py` | Retention defaults, limits, and rollout flags |
| `backend/config/workspace_config.py` | Host/runtime workspace root resolution |
| `backend/config_bootstrap.py` | CLI wrapper for generated configuration |
| `backend/models/core.py` | User, task, engagement, task history/counter, and basic report tables |
| `backend/models/chat.py` | Chat message, tool call, turn event, and legacy agent-log tables |
| `backend/models/cve.py` | CVE settings, sync, record, and product-projection tables |
| `backend/models/data_management.py` | Tenant data-management setting table |
| `backend/models/hitl.py` | Turn workflow and interrupt-ticket tables/enums |
| `backend/models/knowledge.py` | Knowledge ingestion, evidence, entity, relationship, link, and provenance tables |
| `backend/models/llm.py` | LLM credentials, selections, conversations, and usage tables |
| `backend/models/platform_installation.py` | First-run platform installation state |
| `backend/models/provenance.py` | Tool execution, artifact manifest, and execution artifact tables |
| `backend/models/reporting.py` | Closure memo, engagement report, and report-job tables |
| `backend/models/runner_control.py` | Execution site, runner, credential, connection, job, and control-message tables |
| `backend/models/semantic_memory.py` | User/engagement semantic memory rows |
| `backend/models/streaming.py` | Replayable stream event and system log tables |
| `backend/models/tenant.py` | Tenant and membership tables |
| `backend/repositories/tool_execution_repository.py` | Tool-execution provenance queries/writes |
| `backend/repositories/execution_artifact_repository.py` | Execution-artifact provenance queries/writes |
| `backend/repositories/reporting/` | Separated memo, report artifact, requester job, worker job, and retention persistence |
| `backend/schemas/core.py` | User, task, history, agent-log, and basic report API contracts |
| `backend/schemas/data_management.py` | Tenant data-management API contracts |
| `backend/schemas/llm.py` | LLM provider, selection, conversation, and usage API contracts |
| `backend/schemas/network_overview.py` | Network overview response contracts |
| `backend/schemas/reporting.py` | Engagement reporting API contracts |
| `backend/schemas/retention.py` | Retention request/result contracts |
| `backend/schemas/system_metrics.py` | Host resource metric responses |
| `backend/schemas/usage_insights.py` | Usage insights responses |
| `backend/schemas/vpn.py` | VPN and VPN-aware task contracts |

### Router ownership

All rows below are mounted from `backend/main.py`, directly or through a
composed router.

| Router path | Transport owner |
|---|---|
| `backend/routers/setup.py` | First-run setup API |
| `backend/routers/auth.py` | Login, registration, refresh, session/user endpoints |
| `backend/routers/tenants.py` | Tenant membership, selection, and tenant administration APIs |
| `backend/routers/tasks/` | Task CRUD, runtime transitions, interrupts, files, scope, logs, container status, metrics, and VPN APIs; composed by `backend/routers/tasks/router_bundle.py` |
| `backend/routers/chat/` | Chat readiness, history, submit, cancel, and status APIs; composed by `backend/routers/chat/router_bundle.py` |
| `backend/routers/reporting/` | Engagement reporting inputs, reports, jobs, and memos |
| `backend/routers/reports.py` | Existing report API mounted under `/api/reports`; verify callers before extending instead of duplicating reporting routes |
| `backend/routers/runner_control.py` | Execution-site, runner registration/control-channel, job, and runner administration API |
| `backend/routers/artifact_provenance.py` | Execution/artifact provenance reads |
| `backend/routers/engagements_crud.py` | Engagement lifecycle API |
| `backend/routers/engagement_knowledge.py`, `backend/routers/knowledge.py` | Engagement knowledge and knowledge workspace APIs |
| `backend/routers/llm.py` | Provider catalog, credential, selection, conversation, and LLM-facing APIs |
| `backend/routers/usage.py` | Usage insights API |
| `backend/routers/settings.py`, `backend/routers/data_management_settings.py`, `backend/routers/cve_settings.py` | User/platform, retention/data-management, and CVE-index settings APIs |
| `backend/routers/system_metrics.py`, `backend/routers/network_overview.py`, `backend/routers/health.py` | Authenticated observability and readiness APIs |
| `backend/routers/retention.py` | Tenant retention execution API |
| `backend/routers/agent_reasoning.py` | Reasoning stream compatibility/SSE surface |
| `backend/routers/docker_logs.py` | Aggregate compatibility mount for Docker REST, terminal sessions, and deprecated WS aliases |
| `backend/routers/docker_logs_rest.py`, `backend/routers/docker_terminal_sessions.py`, `backend/routers/docker_ws_alias.py` | Focused compatibility adapters; do not add provider-neutral task APIs here |

### Service ownership

| Service path | Responsibility owner |
|---|---|
| `backend/services/artifact/` | Tool-execution provenance, artifact catalog/query, runner-result ingestion, and artifact retention |
| `backend/services/audit/` | Tenant audit envelope construction and metadata redaction |
| `backend/services/auth/` | Refresh/session orchestration beyond JWT primitives |
| `backend/services/chat/` | Messages, transcript reads, turn identity/numbering, tool-call rows, observations, and turn events |
| `backend/services/cutover/` | Deployment cutover/parity certification models and checks |
| `backend/services/cve_indexing/` | CVE source sync, parsing, matching, product projection, leases, scheduler, and index settings |
| `backend/services/data_plane/` | Object-store abstraction, artifact upload/read/browse/export, object keys, and retention |
| `backend/services/docker/` | Local Docker client, container config, lifecycle, exec, logs, metrics, and facade composition |
| `backend/services/embeddings/` | Provider-neutral embedding contracts, profiles, selection, and adapters |
| `backend/services/engagement/` | Engagement access and management workflows |
| `backend/services/knowledge/` | Evidence ingestion/storage, identity, projections, queries, replay, archive, rebuild, and retention |
| `backend/services/langgraph_chat/` | Backend chat-to-LangGraph orchestration: context, intent, routing, handlers, execution, checkpoints, compression, streaming, and run lifecycle |
| `backend/services/llm_provider/` | Credential encryption/storage, catalog, selection, health, runtime configuration, and turn-local client resolution |
| `backend/services/memory/` | Long-term memory storage, retrieval, extraction, embeddings, runtime projection, and retention |
| `backend/services/metrics/` | Backend process metrics and retention helpers |
| `backend/services/notifications/` | Browser-facing task notification event construction |
| `backend/services/platform/` | Installation/setup state, background services, generated artifacts, host metrics, and network overview |
| `backend/services/reporting/` | Report readiness, input inventory, generation jobs/workers, section prompts/rendering, reads, deletion, and retention |
| `backend/services/retention/` | Cross-domain retention policy, scheduling, audit, and orchestration |
| `backend/services/runner_control/` | Runner registry/credentials, assignment, control channel, durable jobs/messages, runtime events, terminal streams, and dispatch |
| `backend/services/runtime_provider/` | Runtime request contracts, authorized context, placement policy, provider registry, operation dispatch, and result normalization |
| `backend/services/runtime_provider/cloud_runner/` | Managed-provider implementation for lifecycle, terminal, artifacts, environment, and tool-command operations |
| `backend/services/streaming/` | Stream schema, in-memory task fanout, persistence/replay, reasoning stores, SSE, and log watching |
| `backend/services/task/` | Task access, admission/quota, lifecycle/state, runtime transitions, interrupts/retry, cleanup, retirement, and retention |
| `backend/services/tenant/` | Tenant context, authorization, membership, bootstrap, dependencies, and RLS session context |
| `backend/services/terminal/` | Terminal contracts, session registry/manager, models, and WS handler |
| `backend/services/usage_tracking/` | Usage extraction, recording, pricing, caching, insights queries, and retention |
| `backend/services/websocket/` | WS authentication gateway, ownership enforcement, connection management, aliases, and channel streamers |
| `backend/services/workspace/` | Local task workspace creation, safe file browsing, runtime file queries, and environment metadata |
| `backend/services/data_management_settings_service.py` | Tenant data-management setting validation/persistence |
| `backend/services/vpn_service.py` | Task VPN configuration orchestration |
| `backend/services/container_utils.py` | Shared local container/workspace lookup helpers |

### Backend compatibility surfaces

| Path | Status | Direction |
|---|---|---|
| `backend/services/unified_docker_service.py` | Active backward-compatible import shim | Put implementation in `backend/services/docker/` |
| `backend/services/terminal_session_manager.py` | Active compatibility facade | Put implementation in `backend/services/terminal/` |
| `backend/routers/docker_logs*.py` | Active legacy/compatibility endpoints | Put new provider-neutral task operations in `backend/routers/tasks/` and the owning service |
| `backend/models/__init__.py` | Active aggregate and compatibility export | Put tables in domain-specific model modules and API shapes in `backend/schemas/` |

## Agent and Graph Map

### Agent package ownership

| Path | Responsibility owner |
|---|---|
| `agent/config.py` | `AgentConfig`, the active process/runtime configuration dataclass |
| `agent/config/` | Subsystem configuration plus compatibility re-export of `AgentConfig` |
| `agent/models.py` | Agent action, target, scope, finding, plan, and execution-result data contracts |
| `agent/execution_strategy.py` | Dependency-leaf `ExecutionStrategy` enum |
| `agent/logger.py` | Structured task/agent logging |
| `agent/planner.py` | Scope-document parsing used by task scope and executor adapter paths |
| `agent/scope_validator.py` | Proposed action/target scope enforcement |
| `agent/executor.py` | Stable execution facade and collaborator composition; transport routing is delegated to `tool_runtime/` |
| `agent/chat/` | Lightweight conversation metadata/index management; DB chat rows remain backend-owned |
| `agent/communication/` | Agent-side JSONL file-communication bridge |
| `agent/context/` | Token accounting, context-window policy, output processing, chunking/index helpers, and context metrics |
| `agent/core/` | Small agent-wide primitives such as time helpers |
| `agent/execution/` | Execution gates and isolated legacy scan helpers |
| `agent/interactive/` | Proposal storage/management for interactive execution |
| `agent/providers/llm/` | Provider-neutral LLM client interface, capabilities, contracts, factory, and OpenAI/Anthropic profiles/adapters |
| `agent/reasoning/` | Active enhanced planner, tool/parameter selection, batch envelopes/commit, and structured recovery |
| `agent/semantic/` | Backend-free semantic metadata extraction/enrichment vocabulary |
| `agent/templates/` | Static report templates with no active production reference found; verify wiring before extending |
| `agent/tool_runtime/` | Tool planning/execution coordination, admission policy, batching, transport choice, timeouts, runtime identity, and result enrichment |
| `agent/tools/` | Tool framework, registry/catalog, schemas, validation, metadata, and concrete tool implementations |
| `agent/utils/` | Agent-wide artifact, output, truncation, and workspace helpers |
| `agent/workspace_init.py` | Runtime workspace initialization copied into the runtime image |
| `agent/environment_validator.py` | Owns the actively consumed `ValidationResult` contract used by scope validation; the `EnvironmentValidator` class itself has no production call site found, so verify that class's wiring before extending it |

### LangGraph placement

| Graph path | Put functions here when they... |
|---|---|
| `agent/graph/state.py` | Define durable/serializable graph state and graph updates |
| `agent/graph/context/` | Build, project, serialize, or select transcript/runtime context |
| `agent/graph/builders/` | Declare graph topology, nodes, and edges |
| `agent/graph/nodes/` | Implement one graph step or node-local helper |
| `agent/graph/routers/` | Select a graph/capability branch without performing the branch's work |
| `agent/graph/subgraphs/` | Implement a reusable multi-node workflow such as tool execution |
| `agent/graph/adapters/` | Bridge graph contracts to executor, persistence, streaming, or tool interfaces |
| `agent/graph/infrastructure/` | Own compiled-graph registry, runtime state models, and stream/event schemas |
| `agent/graph/persistence.py` | Resolve the graph checkpointer surface |
| `agent/graph/emission/` | Build and emit graph events |
| `agent/graph/streaming.py` | Convert execution state/results into stream-facing display data |
| `agent/graph/compression/` | Compact graph/tool/context payloads, including deterministic compactors |
| `agent/graph/memory/` | Working memory, scratchpad, findings, and current-run memory rendering |
| `agent/graph/config/` | Graph-specific limits and configuration |
| `agent/graph/contracts/` | Graph-only shared constants/contracts |
| `agent/graph/utils/` | Reused graph guardrails, progress, retry, scope, todo, and catalog helpers |

### Tool placement

| Tool path | Owner / placement rule |
|---|---|
| `agent/tools/base_tool.py`, `agent/tools/schemas.py`, `agent/tools/exceptions.py` | Framework-wide tool interface and result/error contracts |
| `agent/tools/tool_registry.py` | Tool discovery, lazy loading, registration, and execution lookup |
| `agent/tools/catalog_*`, `agent/tools/capability_surface.py`, `agent/tools/categories.py`, `agent/tools/category_utils.py` | Model-visible catalog policy and category/capability exposure |
| `agent/tools/parameter_*`, `agent/tools/tool_call_specs.py`, `agent/tools/execution_outcome.py` | Framework-wide parameter/call/result contracts |
| `agent/tools/enhanced_metadata*`, `agent/tools/canonical_capture.py`, `agent/tools/utility_metadata.py` | Shared metadata and artifact-capture contracts |
| `agent/tools/filesystem/` | Workspace-safe filesystem tools and their common safety helpers |
| `agent/tools/shell/` | Shell command tools, shell policy, PTY command preparation, and shell contracts |
| `agent/tools/artifact/` | Artifact read/search tools, not artifact persistence |
| `agent/tools/knowledge/` | Knowledge lookup tools such as CVE lookup |
| `agent/tools/pcap_analysis/`, `agent/tools/pcap_compaction/` | Packet-capture classification/correlation and compact result construction |
| Other category packages under `agent/tools/` | Concrete security tool definitions grouped by capability: database, exploitation, forensics, information gathering, access, networking, password attacks, reporting, reverse engineering, sniffing/spoofing, stress, system services, vulnerability analysis, and web applications |

For a new concrete tool, place command construction and tool-specific output
parsing in its category module. Put cross-tool admission, execution-lane, PTY,
file-comm, timeout, or fallback behavior in `agent/tool_runtime/`. Put a shared
tool contract at the narrowest common category boundary before considering the
framework root.

## Runtime and Runner Map

| Path | Responsibility owner | Boundary |
|---|---|---|
| `backend/services/runtime_provider/contracts.py` | Provider-neutral request/result/runtime identity | No Docker, ORM orchestration, or router concerns |
| `backend/services/runtime_provider/operations.py` | Authorized runtime context and operation dispatch | Canonical management-plane side-effect gateway |
| `backend/services/runtime_provider/registry.py` | Placement-to-provider resolution | Fails closed on unknown placement |
| `backend/services/runtime_provider/product_policy.py` | Product placement policy | Independent of provider implementation |
| `backend/services/runtime_provider/local_docker_provider.py` | Local provider adapter | Delegates implementation to `backend/services/docker/` |
| `backend/services/runtime_provider/cloud_runner_provider.py` and `backend/services/runtime_provider/cloud_runner/` | Managed provider adapter/operations | Delegates delivery and durable control state to runner-control services |
| `backend/services/runner_control/` | Management-side runner identity, assignment, jobs, messages, channels, and streams | Does not execute task commands locally |
| `drowai_runner/app.py` | Runner CLI and process composition entry | No backend imports |
| `drowai_runner/cloud_client.py`, `drowai_runner/control_channel/` | Outbound WS connection, protocol session, heartbeat, runtime/tool/terminal/artifact handlers | Network/control-channel owner |
| `drowai_runner/operation_service.py` | Runner-local operation dispatcher | Delegates to lifecycle, command, terminal, metrics, and cleanup owners |
| `drowai_runner/docker_runtime.py` | Runner-side Docker lifecycle/config | Site-local Docker implementation |
| `drowai_runner/workspace.py` | Runner task workspace path/init/cleanup | Task-local path owner |
| `drowai_runner/file_comm_bridge.py`, `drowai_runner/pty_command_transport.py` | Site-side tool transports | Do not add management authorization here |
| `drowai_runner/artifact_*` | Manifest scanning and signed upload execution | Signed URLs and secrets must not enter logs |
| `kali_executor/executor_daemon.py` | Prepared-command process execution and concurrency | Runs inside the task container |
| `kali_executor/communication/file_comm.py` | In-container JSONL queue and lock behavior | Shared filenames/schemas come from `runtime_shared` |
| `runtime_shared/runner_protocol.py` | Backend/runner wire DTOs and parsing | Framework-free |
| `runtime_shared/file_comm_contracts.py` | JSONL messages and workspace queue layout | Agent/runner/executor shared authority |
| `runtime_shared/docker_contracts.py` | Runtime paths, mounts, startup command, resource defaults | Deterministic helpers only |
| `runtime_shared/workspace_filesystem.py` | Race-safe, symlink-safe host workspace I/O | Filesystem trust-boundary primitive |
| `runtime_shared/workspace_files.py` | Runtime file declarations/materialization | Bounded task-relative files only |
| `runtime_shared/runtime_network.py` | Per-task network identity/specification | No Docker SDK calls |
| `runtime_shared/docker_network_manager.py` | Shared backend-free task-network adapter | Owns Docker SDK network operations used by both local and runner providers |
| `runtime_shared/semantic/` | Cross-runtime service/network/web semantic keys and normalization | No tool- or persistence-specific behavior |
| `runtime_shared/terminal_*` | Terminal DTOs, identities, and manager port | No backend terminal implementation |
| `runtime_shared/durable_secret_masking/` | Persistence-sink secret detection/masking | Shared durable-output safety boundary |
| `runtime/image/` | Runtime Dockerfile and Python dependency layer | Only explicitly copied code enters the runtime image |
| `runtime/manifests/` | Runtime asset/version lock contract | Packaging authority |
| `runtime/vpn/` | In-runtime VPN manager script | Execution-plane asset |

## Shared Core Map

| Path | Responsibility owner |
|---|---|
| `core/llm/` | LLM role keys, role/model/reasoning policy, structured schemas, JSON extraction, and timeout contracts |
| `core/prompts/registry.py` | Canonical template and builder registration |
| `core/prompts/loader.py` | Versioned template loading and rendering |
| `core/prompts/builders/` | Prompt construction by reasoning/reporting stage |
| `core/prompts/versions/` | Versioned prompt text selected through `latest.txt` manifests |
| `core/runbooks/` | Runbook discovery, validation, resolution, and rendering used by tool-planning prompts |
| `core/memory/` | Pure shared memory formatting helpers |
| `core/tool_category_taxonomy.py` | Canonical selectable tool categories |
| `core/tool_capability_taxonomy.py` | Canonical prompt-facing capability families |

Do not place a helper in `core/` merely because both backend and agent use it.
First decide whether it is prompt policy, LLM policy, runbook behavior, memory
formatting, or a tool taxonomy. If it is a cross-process runtime contract, it
belongs in `runtime_shared/` instead.

## Frontend Map

| Path | Responsibility owner | Placement rule |
|---|---|---|
| `client/src/main.tsx` | React mount | Composition only |
| `client/src/App.tsx` | Provider shell and top-level routes | Add route wiring here; keep page behavior in pages/features |
| `client/src/pages/` | Route-level screen composition | One page owner per route surface |
| `client/src/features/` | Self-contained product capabilities | API/types/state/components that belong to one capability; currently includes LLM provider UI |
| `client/src/components/ui/` | Generic visual primitives | No product/API policy |
| `client/src/components/<domain>/` | Reusable domain presentation | Keep cross-domain transport/state elsewhere |
| `client/src/components/runtime/` | Runtime stream bootstrap/composition | No protocol implementation |
| `client/src/hooks/` | React lifecycle and state integration | Extract non-React protocol/service logic to `services/` |
| `client/src/services/runtime_stream/` | Multiplex WS client, lifecycle, subscription planning, packet ingestion, and protocol types | No React/UI rendering |
| `client/src/state/` | Task/chat/workbench/notification/retry/context-window client stores | Task-key state; transport clients write through defined store functions |
| `client/src/lib/api-config.ts` | API base URL, authenticated fetch, auth recovery, tenant headers | Canonical HTTP transport entry |
| `client/src/lib/queryClient.ts` | React Query client and compatibility request wrapper | New code should prefer the canonical API helper surface |
| `client/src/lib/auth-session.ts`, `client/src/lib/tenant-context.ts`, `client/src/lib/tenant-query-cache.ts` | Browser auth/tenant session and cache isolation | Never persist server secrets |
| `client/src/navigation/` | Route constants, destination registry, and app search | Feature/page owners provide their destinations |
| `client/src/types/` | Cross-feature wire/view contracts | `types/packets.ts` is generated; do not edit it manually |
| `client/src/utils/` | Pure formatting/normalization/export helpers | No network or component lifecycle |
| `client/src/config/` | Frontend-visible feature/settings configuration | Backend remains policy authority |
| `client/src/contexts/` | React context spanning multiple surfaces | Use only when a store or query cache is not the correct owner |
| `client/src/assets/`, `client/src/index.css` | Static assets and global styling | Presentation only |

Backend stream schema changes must update the generator in
`backend/scripts/generate_streaming_types.py` and regenerate
`client/src/types/packets.ts`.

## Tests, Build, and Operations Map

| Path | Owner |
|---|---|
| `backend/tests/` | Backend router/service/model/config tests; includes focused integration and security subtrees |
| `client/src/__tests__/` and colocated client `__tests__/` | Frontend routing, component, hook, service, state, and utility tests |
| `agent/tests/`, `agent/graph/**/tests/` | Tool, planner, graph, runtime, and agent contract tests |
| `core/**/tests/` | Prompt, LLM policy, and core contract tests |
| `tests/runner/`, `tests/drowai_runner/` | Managed-runner behavior |
| `tests/runtime_shared/` | Shared runtime contracts and safety primitives |
| `tests/runtime_image/` | Packaged runtime/container contract |
| `tests/scripts/` | Build/package/launcher scripts |
| `tests/integration/` | Cross-package execution/tool integration |
| `tests/context/` | Context ingestion, redaction, splitting, and compaction tests |
| `tests/e2e/` | Playwright/CI contract and Kali web-tool integration tests |
| `tests/fixtures/` | Shared HTTP, VPN, and web-tool fixture payloads |
| `tests/tools/` | Broad tool behavior and schema contracts |
| `kali_executor/tests/` | In-container executor daemon tests |
| `e2e/fixtures/` | Deterministic browser actors, seed, auth, task, chat, and runtime fixtures |
| `e2e/tests/` | Product journeys and authorization/isolation checks |
| `e2e/probes/` | Failure/artifact probes with separate Playwright configuration |
| `e2e/reporters/` | Secret-safe browser artifacts |
| `scripts/run_release_gate.py` | Release tier selection |
| `scripts/run_langgraph_*` | LangGraph regression and prompt-mock suites |
| `scripts/build_*`, `package_*`, `verify_*` | Runtime/runner packaging and verification |
| `scripts/local_production_cloud.py` | Canonical local-development launcher; use `python3 scripts/local_production_cloud.py up` |
| `deploy/compose/`, `deploy/cloud/` | Product deployment manifests |
| `deploy/env/` | Checked-in deployment environment examples, never real secrets |
| `deploy/images/`, root `Dockerfile.*` | Control-plane, frontend, and runner images |
| `deploy/postgres/` | PostgreSQL initialization assets such as pgvector enablement |
| `deploy/scripts/` | Standalone install, runner-token, and profile-verification commands |
| `deploy/env_contract.py` | Shared product/runner deployment environment defaults |

Place a test with the narrowest owner. Use root `tests/` only when a test spans
packages, validates packaging, or exercises a compatibility/public boundary.

## Generated, Local, and Non-Source Paths

These directories may exist in a working tree but are not destinations for
source changes.

| Path | Classification |
|---|---|
| `dist/`, `build/` | Generated build output |
| `agent/workspaces/`, root `workspace/` | Local task runtime workspaces |
| `artifacts/`, `wordlists/`, `output/` | Local tool/runtime output |
| `.drowai-local/` | Local generated configuration and secrets |
| `.drowai-runner/`, `.drowai-runner-cloud/` | Local runner jobs, credentials, logs, and task state |
| `agent/management_state/`, `backend/management_state/` | Local management-plane indexes/cache/locks |
| `agent/durable_knowledge/` | Local durable agent data |
| `.playwright-cli/`, `e2e/output/`, `test-results/` | Browser/test output |
| `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `node_modules/` | Tool/dependency caches |

If a behavior is visible only in one of these paths, find the tracked code that
creates or consumes it before changing anything.

## Function Placement Rules

Use this order when a function could fit in several places:

1. **Find the owner of the state or side effect.** Put the function beside that
   owner: task lifecycle, runner job, graph state, workspace, stream, and so on.
2. **Separate transport from orchestration.** Routers/components adapt input;
   services/hooks coordinate; domain/pure helpers decide.
3. **Respect the process boundary.** Shared backend/runner/runtime data belongs
   in `runtime_shared/`; site execution belongs in `drowai_runner/`; container
   execution belongs in `kali_executor/`.
4. **Respect runtime dispatch.** New management-plane task runtime operations
   should enter through `RuntimeOperationService`; provider implementations own
   provider details. The active graph tool-dispatch runner and local lanes are
   existing exceptions described above, not universal service wiring.
5. **Keep policy at one authority.** Prompt policy goes to `core/prompts`, LLM
   role policy to `core/llm`, tool category policy to the canonical taxonomy,
   and deployment policy to config/env contracts.
6. **Keep helpers narrow.** A one-owner helper stays private in that module. A
   package-wide helper goes in that package. Promote it only when real callers
   cross the package boundary.
7. **Do not add behavior to compatibility or aggregate modules.** Delegate to
   the canonical package and preserve the old import/route only if required.
8. **Mirror the owner in tests.** Test the narrow behavior at the narrow owner;
   add integration coverage only for the boundary crossed.

## Updating This Compass

Update this file when a change introduces a new top-level package, capability
owner, process entrypoint, provider boundary, compatibility surface, generated
contract, or source/runtime-state distinction. A new individual file inside an
already accurate owner row does not require a compass update.

Before editing a row, verify it through an active import, router mount, process
entrypoint, package script, runtime-image copy rule, or direct caller. Directory
names and older documentation alone are not evidence that code is wired.
