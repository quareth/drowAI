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
- Local development and deployment workflows, architecture documentation,
  contribution guidance, and private vulnerability reporting.

### Changed

- The frontend toolchain now requires Node.js 20.19 or newer and uses
  security-patched Vite dependencies.
