# Changelog

Notable user-facing changes to DrowAI are recorded here. DrowAI is pre-v1, so
interfaces and deployment workflows may change between development releases.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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
