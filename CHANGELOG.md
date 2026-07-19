# Changelog

Notable user-facing changes to DrowAI are recorded here. DrowAI is pre-v1, so
interfaces and deployment workflows may change between development releases.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Target version: `0.1.0`.

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
- GPT-OSS 20B appears in the curated LLM catalog with reviewed routes for
  NVIDIA, Hugging Face, Ollama, and vLLM, plus deployment-aware runtime routing.
- Deployment-aware LLM management now uses reviewed catalog and connection
  preset manifests, supports scaled compatible connection/deployment inventory
  with custom model registration, service-authorized refresh, usage/pricing
  attribution, and explicit deployment selection contracts.
- GPT-OSS 20B deployment choices now appear under one canonical model entry
  while preserving explicit provider-specific deployment selection.
- Local development and deployment workflows, architecture documentation,
  contribution guidance, and private vulnerability reporting.

### Changed

- Dependency security updates refresh the frontend and backend toolchains,
  replace python-jose with PyJWT, and require Node.js 20.19 or newer.
- The chat model selector now groups GPT-OSS 20B under Open models and shows
  each ready serving route as an explicit Run with choice.
- Provider settings place the reporting model preference first, followed by
  direct credentials and intentionally supported GPT-OSS 20B routes;
  deployment, capability, lifecycle, and proving internals are no longer shown.
- The canonical local launcher is now `scripts/local_dev.py`; startup can
  interactively provision its PostgreSQL login role, database, and pgvector
  extension before running migrations.

### Fixed

- Provider settings now render one setup card per supported GPT-OSS route
  instead of duplicating cards for provider inventory models.
