# Changelog

Notable user-facing changes to DrowAI are recorded here. DrowAI is pre-v1, so
interfaces and deployment workflows may change between development releases.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.0] - 2026-08-18

### Added

- Main agents and subagents can now run one-shot commands or maintain
  interactive shell sessions inside their task's Kali runtime, with exact stdin
  handling, runtime-managed waiting, bounded output, and lifecycle cleanup.

### Changed

- Shell utility output is transient while shell assessment output remains
  eligible for durable artifacts, provenance, and Knowledge retention.
- Interactive shell policy now reserves hard blocks for obvious environment
  destruction, shutdown, resource exhaustion, and container escape while
  permitting pentesting workflows in the default permissive mode.
- The inert `SHELL_EXEC_MAX_COMMAND_CHARS` agent setting has been retired;
  command length alone remains accepted and no replacement limit was added.
- Tool-output compression now allows up to 4,096 output tokens while preserving
  downstream context limits.
- Pathfinder now has nine tool-capable iterations and uses a plain final
  handoff, improving compatibility after its tool budget is exhausted.
- Agent and UI terminals now start in the task runtime's `/workspace`
  directory, and fallback planning retains the complete visible tool catalog.

### Fixed

- Fresh ARM64 and AMD64 runtime-image builds now remain compatible with Kali
  rolling's Python 3.14 package set.
- Messages sent while a subagent handoff is still running now remain queued
  behind the active parent turn, including across repeated approval pauses.
- Distributed runner packages now install the process-inspection dependency
  required by the managed runner at startup.
- The distributed frontend health probe now uses the IPv4 loopback address,
  avoiding false unhealthy status when `localhost` resolves to IPv6.
- Shell commands now preserve authoritative completion, exit codes, and
  interaction state across local and managed runtimes, preventing duplicate
  execution and premature finalization.
- Interrupted, cancelled, timed-out, and transport-failed shell sessions now
  remain accurately controllable and visible until the runtime reports their
  terminal outcome, including during cleanup and backend shutdown.
- Interactive shell output now preserves final chunks, line breaks, meaningful
  whitespace, multibyte UTF-8 characters, and legitimate exit-token text across
  live streaming, completion, refresh, and replay.
- Shell activity cards now remain correlated with their originating command and
  show consistent running and terminal states, including `Processing result…`
  while final reasoning is still underway.
- Completed shell assessments now expose one provider-verified durable
  transcript and artifact set, while utility commands remain transient and
  cleanup preserves eligible evidence.
- Interactive sessions now respect bounded waits, output limits, global
  deadlines, runtime identity, and same-session serialization without blocking
  independent sessions or expiring active work.
- Main agents and subagents now sequence dependent shell stages, avoid false
  free-form parameter retries, and reject ambiguous batches that attempt to
  start multiple live shell sessions.
- Direct execution now honors valid Pathfinder delegation selected by
  post-tool reasoning instead of prematurely finalizing the response.
- Sequential subagent handoffs now retain parent goal, todo, and reasoning state
  while returning bounded child summaries, and refreshed conversations preserve
  parent, subagent, and observation ordering.
- Subagents now retain bounded prior tool outcomes, adapt parallel tool calls to
  the selected model, recover from invalid actions, and distinguish interrupted
  infrastructure from failed agent work.
- Temporary LLM API failures now receive bounded provider-neutral retries while
  preserving the selected provider and offering checkpoint retry after
  exhaustion.
- Local development runners now follow verified launcher ownership, preserve
  shutdown recovery across legacy PID files, and avoid terminating unrelated or
  standalone processes.
- Managed-runner reconnect handshakes now complete before stale terminal cleanup,
  preventing multi-session cleanup from exhausting the runner's open timeout.
- ANSI-formatted ffuf results no longer create invalid Knowledge identities or
  block task deletion after evidence is safely archived.

### Security

- Updated cryptography, Undici, and PostCSS to patched releases that address
  dependency security advisories.
- Managed-runner terminal streams now reject data for unknown or closed sessions
  and keep buffered output bounded.
- Shell environment inputs require valid variable names, durable history masks
  stdin values, and destructive commands remain blocked through supported
  wrappers and continued lines.

## [0.3.0] - 2026-08-04

### Added

- DrowAI can delegate task-scoped work to declarative subagents. Pathfinder is
  the first built-in specialist and performs bounded reconnaissance before
  returning its evidence and findings to the parent agent. Active Pathfinder
  runs remain process-local and are marked interrupted after a backend restart
  rather than being recovered automatically.
- Subagent runs now appear as attributed activity cards with a dedicated drawer
  for active and completed runs, isolated transcripts, lifecycle status,
  approvals, cancellation, and refresh-safe replay.
- The Operations task panel can be resized within a bounded range, collapsed
  from its divider or toolbar control, and reopened from the conversation
  header without remounting the chat.
- Kali task runtime images now include Amass, DNSRecon, enum4linux-ng, Hydra,
  Nuclei with a bundled template snapshot, and WhatWeb, with adapters updated
  for the installed Amass and enum4linux-ng command interfaces.
- Amass is available in the LLM-visible tool catalog for DNS enumeration, with
  normalized DNS/IP knowledge assets and `resolves_to` relationships.
- Repo-local Codex workflows can deliver one Phase 01 milestone tool per branch
  and pull request with wired-path analysis, real-Kali and GUI mechanical
  validation, phased reviews, and branch-scoped quality gates.

### Changed

- Pathfinder ownership routing and its card and drawer interface are enabled by
  default without backend or frontend feature-flag configuration.
- Parent reasoning now waits for all delegated runs to finish, incorporates
  their persisted evidence and knowledge, and continues with the remaining
  execution budget before finalizing or selecting follow-up work.
- Pentest observations now flow through a shared canonical fact pipeline for
  deterministic summaries, durable Knowledge, and Territory views.

### Fixed

- Subagent cancellation, approval continuation, replay, repeated invocations,
  and terminal state now remain scoped to the correct task and run without
  reviving stale approvals or orphaned activity.
- Delegated tool results and evidence now remain available to parent reasoning
  without repeated execution, lost progress, or reset safety budgets.
- Final answers stream provider text chunks live instead of appearing only
  after a buffered response completes.
- Canonical pentest processing now rejects non-finite evidence, excludes closed
  or filtered Masscan rows from open-service facts, preserves Nmap service
  states, strips URL credentials, retains IPv6 and hostname-backed web paths,
  and prioritizes findings and services within bounded evidence summaries.
- FFUF and HTTP web surfaces, TShark credential-exposure findings, compact
  omission counts, and linked Knowledge details now remain consistent across
  deterministic summaries and Territory views.
- Queued prompts now remain visible and scrollable in constrained Overview
  layouts, keep their controls reachable, and advance exactly one item after
  each completed run.
- Profile and Settings now share an accessible contextual back control that
  returns to the originating in-app page and safely falls back to Outpost for
  direct entries.
- Amass now reuses serialized task-scoped v5 state across enumeration and
  result queries, returns stored partial results after bounded enumeration,
  and distinguishes parser status, enumeration status, completeness, and
  seed/prior/new DNS names.
- Amass deterministic summaries now apply shared bounded finding limits with
  explicit total, shown, and omitted DNS-detail counts instead of silently
  dropping mappings.
- API settings now render every hosted catalog provider through the shared
  provider card and connection status, including reviewed compatible models.

### Security

- JWT signing rejects configured HS256 secrets shorter than 32 bytes and
  automatically repairs legacy short generated secrets during bootstrap.

## [0.2.0] - 2026-07-24

### Added

- A provider-neutral live agent compatibility harness now exercises DrowAI's
  production intent schema, Nmap tool contract, local argument validation, and
  post-tool response lifecycle without executing the security tool. OpenAI
  GPT-5.4 Mini and Mistral Small 4 form the reviewed compatibility matrix.
- Mistral Small 4 is available as a reviewed compatible deployment with
  encrypted UI-managed credentials, guarded inference, tool use, adjustable
  reasoning, usage pricing, and the normal deployment selection lifecycle.
- GPT-OSS 20B appears in the curated LLM catalog with reviewed routes for
  NVIDIA, Hugging Face, Ollama, and vLLM, plus deployment-aware runtime routing.
- Deployment-aware LLM management now uses reviewed catalog and connection
  preset manifests, supports scaled compatible connection/deployment inventory
  with custom model registration, service-authorized refresh, usage/pricing
  attribution, and explicit deployment selection contracts.
- GPT-OSS 20B deployment choices now appear under one canonical model entry
  while preserving explicit provider-specific deployment selection.
- Repository-local implementation quality review and fixer workflows can audit
  a frozen branch or commit scope before publication.

### Changed

- Ollama and vLLM self-hosted settings and model choices are hidden by default
  until operator-controlled private-network registration is available.
- Agent-turn roles now consistently use the user-selected deployment across
  OpenAI, Anthropic, and compatible models; lightweight internal calls use a
  shared low-effort policy instead of silently switching models.
- GPT-OSS 20B now runs classification, planning, structured responses,
  function/tool calls, compression, post-tool reasoning, and streamed
  articulation through the user-selected serving route instead of switching
  hidden agent roles to another provider's model.
- Native and hosted model routes accept provider-scoped operator base URLs for
  gateways or local development without changing other connection endpoints.
- The chat model selector now groups GPT-OSS 20B under Open models and shows
  each ready serving route as an explicit Run with choice.
- Provider settings place the reporting model preference first, followed by
  direct credentials and intentionally supported GPT-OSS 20B routes;
  deployment, capability, lifecycle, and proving internals are no longer shown.

### Fixed

- Updating an LLM connector now replaces its existing credential and
  configuration without creating duplicate connections or changing saved
  deployment selections.
- Explicit deployment routes now select their registered adapter independently
  of model vendor, and non-native model output ceilings are no longer converted
  into oversized implicit request budgets.
- Task cards now refresh automatically while lifecycle operations are in
  progress instead of remaining stuck on transitional statuses such as
  Starting after the runtime is ready.
- Reporting and chat model preferences now preserve deployment identity across
  native, hosted open-model, and self-hosted routes instead of misreading a
  serving connection as a canonical model provider.
- Deployment-backed models with a single route now open reasoning choices
  directly instead of showing a redundant provider submenu.
- Runtime-selected OpenAI-compatible models now keep graph reasoning and HITL
  resume events live while providers are working, stream response chunks
  incrementally, and reliably clear completed response indicators.
- LangGraph resume and retry now preserve the checkpointed deployment across
  approval pauses, reject conflicting or malformed runtime identity, and avoid
  switching to a user's newer default model.
- Provider settings now use one consistent card, connection status, and API-key
  control layout across native, hosted open-model, and self-hosted routes.
- Provider settings now show concise invalid-key and permission errors for
  hosted open-model connections instead of raw API response payloads.
- OpenAI-compatible models that return requested function calls as JSON message
  content are now safely normalized against the requested tool contracts,
  avoiding spurious structured-validation failures.
- Reviewed GPT-OSS routes now use one agent-capable compatible protocol contract
  across NVIDIA, Hugging Face, Ollama, and vLLM; arbitrary custom compatible
  endpoints remain conservative and fail closed.
- LangGraph usage records now retain the selected connection, deployment, and
  route identity even when final graph metadata omits the runtime selection.
- Provider settings now render one setup card per supported GPT-OSS route
  instead of duplicating cards for provider inventory models.
- Chat requests now use usage-tracked non-streaming responses when a selected
  model route cannot report token usage during streaming.
- Chat model selection now prevents unbound provider models from being chosen
  until the required API credential is configured.

### Security

- Managed LLM endpoints using the same connection preset now retain isolated
  credentials, preventing one endpoint from receiving another endpoint's key.

## [0.1.0] - 2026-07-16

### Added

- FastAPI control plane for authentication, tenants, task lifecycle, chat,
  reporting, knowledge and evidence, settings, and realtime WebSocket and SSE
  channels.
- React and TypeScript interface for setup, task operation, streaming chat,
  artifacts, terminals, knowledge, reports, usage, profiles, and settings.
- LangGraph-based agent orchestration with managed prompts, tool policy,
  structured tool results, and task-scoped execution state.
- Provider-neutral task execution through local Docker or managed runners,
  including per-task Kali runtimes and isolated workspaces.
- Local development and deployment workflows, architecture documentation,
  contribution guidance, and private vulnerability reporting.

### Changed

- Dependency security updates refresh the frontend and backend toolchains,
  replace python-jose with PyJWT, and require Node.js 20.19 or newer.
- The canonical local launcher is now `scripts/local_dev.py`; startup can
  interactively provision its PostgreSQL login role, database, and pgvector
  extension before running migrations.

[Unreleased]: https://github.com/quareth/drowAI/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/quareth/drowAI/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/quareth/drowAI/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/quareth/drowAI/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/quareth/drowAI/releases/tag/v0.1.0
