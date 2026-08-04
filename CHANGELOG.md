# Changelog

Notable user-facing changes to DrowAI are recorded here. DrowAI is pre-v1, so
interfaces and deployment workflows may change between development releases.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Repo-local Codex workflows can deliver one Phase 01 milestone tool per
  branch and pull request with wired-path analysis, real-Kali and GUI
  mechanical validation, phased reviews, and branch-scoped quality gates.
- Kali task runtime images now include Amass, DNSRecon, enum4linux-ng, Hydra,
  Nuclei with a bundled template snapshot, and WhatWeb, with adapters updated
  for the installed Amass and enum4linux-ng command interfaces.
- Amass is available in the LLM-visible tool catalog for DNS enumeration, with
  normalized DNS/IP knowledge assets and `resolves_to` relationships.
- Pathfinder recon routing and its card/drawer UI are now active by default without
  backend or frontend feature-flag configuration.
- The Pathfinder recon-agent pilot UI now reconstructs recent cards from task stream
  replay, preserves their original lifecycle timing, rechecks process-local ownership
  after stream reconnection, and marks replay-only active runs as interrupted when the
  current backend no longer owns them.
- Pathfinder now uses a dedicated colored identity mark across its parent event,
  drawer list, and detail header instead of the generic robot icon.
- The Operations task panel can now be resized within a bounded range, collapsed
  from its divider or toolbar control, and reopened from the expanded
  conversation header without remounting the chat.

### Changed

- Pathfinder subagent execution now runs through the declarative generic
  subagent runtime without the old Scout-specific runtime modules.
- Completed subagent handoffs now pass through parent post-action reasoning
  after every scoped run is terminal and before the parent finalizes, delegates
  follow-up work, or calls a tool.

### Fixed

- Masscan semantic projection now excludes explicitly closed or filtered rows
  from canonical open-port and detected-service facts.
- Knowledge service projection now preserves canonical open, closed, and
  filtered network-service states in durable inventory and topology views.
- HTTP and FFUF semantic facts now strip URL userinfo before durable
  persistence and preserve bracketed IPv6 literals in canonical web identity.
- Compact canonical pentest projections now retain result summaries and
  matcher/filter context ahead of routine execution parameters when evidence
  budgets are exceeded.
- Compact canonical pentest projections now retain findings and services ahead
  of lower-priority asset facts when fact budgets are exceeded.
- Canonical pentest facts now preserve FFUF and HTTP web surfaces, TShark
  credential-exposure findings, compact omission accounting, and linked
  Knowledge details across deterministic processing and Territory views.
- Stopping a subagent during approval continuation now cancels the resumed
  child without allowing it to complete, while parent-turn cancellation still
  stops the parent flow and lifecycle publication failures no longer strand a
  completed child result.
- Parent post-handoff tool approvals now resume the same checkpointed reasoning
  flow after an earlier subagent approval in the same turn, without losing
  coordination state or replaying a failed approval request.
- Cancelling a main or subagent run that is waiting for approval now retires
  its pending approval ticket, preventing stale approvals from being resumed or
  reused by later interrupts.
- Interrupted subagent runs now remain terminal when delayed replay or live
  events repeat an equal lifecycle version, preventing orphaned runs from
  reappearing as active after a backend restart.
- Parent continuation now waits for every relevant subagent run to become
  terminal before evaluating their aggregated handoffs, preventing partial
  batches from finalizing a task while sibling agents are still running.
- Final answers now stream provider text chunks live again instead of appearing
  only after a buffered structured response completes.
- Process-local subagent replay now accepts the backend-owned response marker,
  allowing orphaned nonterminal runs to reconcile to interrupted state.
- Post-action reasoning now applies its complete progress, repetition, retry,
  and stopping policies while committing routes through `ptr_commit`, restoring
  reflection or finalization after repeated no-progress actions.
- Blocked direct-executor turns now retain grounded task seeds so recovery
  attempts remain visible to progress tracking and the three-phase stall guard.
- Subagent tool calls now inherit the parent Agent or Full Access approval
  policy, reuse the existing main conversation approval card, and show only a
  waiting indicator in the subagent drawer.
- Parent continuation after a completed subagent handoff now preserves its
  remaining execution budget, allowing required follow-up tools to run without
  resetting safety limits between handoff cycles.
- Persisted tool results from both parent and subagent execution now trigger
  durable knowledge ingestion at shared tool completion, independent of whether
  parent post-action reasoning receives the child execution state.
- Post-action Observations now use the same provider-neutral model turn that
  commits the next graph route. Optional narration, larger output budgets,
  truncation detection, and commit-only recovery prevent completed external
  work from being repeated when an internal route commit is incomplete.
- Each parent post-action phase now receives a distinct Observation stream
  identity, so later phases no longer overwrite or reorder an earlier card.
- Parent post-action reasoning now uses one provider-portable internal commit
  schema for all routes, aligned with runtime validation while malformed
  optional knowledge candidates are safely discarded.
- Parent reasoning now receives completed handoff evidence from the canonical
  context bundle, preventing redundant delegation after a subagent has already
  completed the requested work.
- Pathfinder now returns a parent handoff after successful tool evidence instead
  of treating remaining iteration budget as a requirement to run the tool again.
- Repeated subagent invocations in one conversation now remain separate drawer
  rows keyed by run identity, with each row opening only its own transcript.
- Pathfinder now shows its real pre-tool action-selection step as an attributed,
  refresh-safe Thinking event before tool execution without adding another
  model call.
- Pathfinder now binds its complete bounded recon tool profile directly and reuses
  the shared native call-builder guidance, allowing up to the configured batch
  limit of concrete sequential or parallel calls without a redundant selector.
- Intent classification now emits an explicit ordered `agent_handoffs`
  contract for subagent routing. Pathfinder delegation no longer depends on
  `suggested_capabilities` vocabulary, while those capabilities remain
  available as advisory assignment context. Enabled agent names, ownership
  boundaries, target requirements, concurrency, classifier catalog entries,
  schema constraints, and dispatch branches now come from one subagent
  registry instead of hardcoded prompt-local rules.
- Subagent calls now participate in the parent turn's ordered activity chain by
  run identity and stream sequence, regardless of the implemented subagent kind,
  and completed-turn summaries count each distinct run as an agent.
- Pathfinder handoff responses now stop their streaming indicator when the
  answer section closes and return a bounded child result from the generic
  runtime loop for parent reasoning.
- JWT signing now rejects configured HS256 secrets shorter than 32 bytes and
  automatically repairs legacy short generated secrets during bootstrap.
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
- Pull-request and post-merge browser journeys now verify that hosted catalog
  providers appear exactly once in API settings with consistent connection
  status.

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

[Unreleased]: https://github.com/quareth/drowAI/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/quareth/drowAI/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/quareth/drowAI/releases/tag/v0.1.0
