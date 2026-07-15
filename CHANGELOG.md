# Changelog

Notable user-facing changes to DrowAI are recorded here. DrowAI is pre-v1, so
interfaces and deployment workflows may change between development releases.

The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Target version: `0.1.0`.

### Added

- Public contribution guidance and a private vulnerability-reporting policy.

### Changed

- Frontend startup is now more responsive, and repeat deployments reuse cached
  assets without adding navigation loading screens.
- Local development now uses the PostgreSQL-backed managed-runner parity
  launcher as its canonical startup path.
- Agent runs now combine consecutive reasoning updates into one timeline-style
  Thinking card while preserving tool and observation boundaries.
- Project and API metadata now consistently identify DrowAI as version `0.1.0`.
- Credentialed browser access now uses an explicit, configurable CORS origin
  allowlist instead of a wildcard.
- Local setup guidance now distinguishes environment configuration from model
  credentials configured through the setup UI.

### Fixed

- Context-window counters now retain the latest measured turn across reloads,
  ignore stale hydration or streaming updates, and refresh bootstrap estimates
  after stream completion when no measurement exists yet.
- Legacy encryption-key files with trailing whitespace no longer rotate valid
  keys and invalidate stored LLM credentials.
- Generated configuration now rejects placeholder or malformed encryption
  secrets and recovers from invalid legacy key files without persisting them.
- Python wheel installs now include versioned prompt templates and builtin tool
  runbooks required by the package-relative runtime loaders.
- Runtime image rebuilds now preserve `fping`, and tagged runtime images refresh
  before new task containers start.
- Local managed-runtime startup now defers missing task runtime image pulls
  until task materialization instead of failing before services start.

### Removed

- The legacy SQLite local launcher and its npm startup shim have been removed.
