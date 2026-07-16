# AGENTS.md
Guidance for **LLM coding assistants** working in this repository.

This repo evolves quickly and some docs drift. **Code is the source of truth**: validate behavior in the wired paths before changing docs or making architectural claims.

## Core principles (apply to every task)
1. Think before coding
- Don’t assume. State assumptions explicitly.
- If something is unclear, stop and ask.
- If multiple interpretations exist, present them—don’t pick silently.

2. Simplicity first
- Minimum code that solves the problem. Nothing speculative.
- No abstractions for single-use code.
- If 200 lines could be 50, rewrite it.

3. Surgical changes
- Touch only what the request requires.
- Don’t refactor adjacent code “because you’re there”.
- If your change creates unused imports/vars, remove those (but don’t delete pre-existing dead code unless asked).

4. Goal-driven execution (TDD)
- Define verifiable success criteria.
- Prefer: reproduce with a test → fix → keep the test.

5. Add docstring to front of the each newly created module, which explains purpose and responsibility of the file briefly.
- If you see any file/module that haven't any docstring, understand the module and its purpose and add docstring.

6. Separation of Concerns, Modularity

- Do not create monolithic code, if you see code that violates Separation of Concerns inform the user.

7. Before editing any file, read the first 20 lines. Most of the files have docstring at the start that explains the responsibility of that file. Read that docstring and be sure you are editing the correct file.

## Secure by Design

- Follow Secure by Design principles. If something user requested causes a security breach, notify the user.
# Principles: Least Privilege, Depth in Defense, Separation of Duties, Segmentations

## Architectural boundaries (Clean Architecture-ish)
Use these boundaries as a *guide* (the codebase isn’t perfectly layered):

┌─────────────────────────────────────┐
│ Presentation                        │  FastAPI routers, HTTP/WebSocket adapters
├─────────────────────────────────────┤
│ Application                         │  Orchestration/services, workflows
├─────────────────────────────────────┤
│ Domain                              │  Task/user concepts, state machines
├─────────────────────────────────────┤
│ Infrastructure                      │  DB, Docker, external APIs
└─────────────────────────────────────┘

Rules of thumb:
- Dependencies point inward.
- Keep routers thin; put orchestration in `backend/services/`.
- Keep control-plane, data-plane, and execution-plane responsibilities separate.
- Do not bypass the runtime-provider boundary. Control-plane code authorizes, records, and dispatches; execution-plane code runs task work through local Docker or managed runner providers.

                          TASK-ISOLATED EXECUTION MODEL

+---------------------------------------------------------------------------------------+
|                             Shared Control Plane (Global)                             |
| AuthN/AuthZ | API Routing | Tenant/Task Registry | Orchestrator | Stream Bus          |
| Runner Control | Runtime Provider Dispatch                                             |
|                                                                                       |
| Contract: runtime side effects use tenant_id + task_id + runtime identity;          |
| streams remain task-keyed after tenant/user authorization.                          |
+-----------------------------------+-------------------------------+-------------------+
                                    |                               |
                                    v                               v

                    +--------------------------------+   +--------------------------------+
                    |        Task Context A          |   |        Task Context B          |
                    |        (task_id = A)           |   |        (task_id = B)           |
                    |--------------------------------|   |--------------------------------|
                    | - Task state                   |   | - Task state                   |
                    | - Tenant/user scope            |   | - Tenant/user scope            |
                    | - Workspace refs/files         |   | - Workspace refs/files         |
                    | - Runtime placement/provider   |   | - Runtime placement/provider   |
                    | - Task event stream            |   | - Task event stream            |
                    | - Task interrupt state         |   | - Task interrupt state         |
                    | - Task approval/resume channel |   | - Task approval/resume channel |
                    +--------------------------------+   +--------------------------------+

Execution Plane (per task): local Docker runtime OR managed runner runtime.

Isolation Rules (applies to all task features):
1) Read/Write scope is task-local (A cannot read/write B task state or runtime channels).
2) Control actions are task-bound (an action for A is resolved only in A context).
3) Interrupt/approval flow is task-bound (A interrupt lifecycle is independent from B).
4) Failures, pauses, resumes, and streams are contained to their own task context.
5) Runtime side effects go through `backend/services/runtime_provider/*`; routers and graph nodes must not call Docker or runner internals directly.


## What this repo is (high-level, code-verified)
- **Backend (`backend/`)**: FastAPI control plane. Owns auth, tenant/task lifecycle, streaming (SSE/WS), chat, persistence, runner control, and runtime-provider dispatch.
- **Agent runtime (`agent/`)**: Python agent + tools centered on LangGraph graphs, tool policy, and provider-neutral runtime/tooling modules.
- **Kali executor (`kali_executor/`)**: runs inside task execution environments; executes prepared tool commands through runtime transports such as local file-based JSONL comms.
- **Frontend (`client/`)**: React + TS UI, uses JWT in Authorization headers and WebSocket subprotocols.

### High-signal entrypoints (start here when validating behavior)
- Backend app + WebSocket multiplexer: `backend/main.py`
- Auth (JWT): `backend/auth.py`, `backend/routers/auth.py`
- Tenancy: `backend/services/tenant/context.py`, `backend/routers/tenants.py`
- Tasks + lifecycle: `backend/routers/tasks/__init__.py`, `backend/services/task/lifecycle_service.py`, `backend/services/task/runtime_service.py`
- Runtime provider boundary: `backend/services/runtime_provider/registry.py`, `backend/services/runtime_provider/contracts.py`
- Runner control: `backend/routers/runner_control.py`, `backend/services/runner_control/*`
- Local Docker provider: `backend/services/runtime_provider/local_docker_provider.py`, `backend/services/docker/*`
- Streaming hub (SSE/WS fanout): `backend/services/streaming/in_memory_hub.py`
- Chat/LangGraph facade: `backend/routers/chat/`, `backend/services/langgraph_chat/facade.py`
- Prompt management surface: `core/prompts/registry.py`, `core/prompts/loader.py`, `core/prompts/builders/*`, `core/prompts/versions/*`
- Workspace layout: `backend/config/workspace_config.py`
- Agent graph/tool runtime: `agent/graph/*`, `agent/tool_runtime/*`
- Workspace-safe filesystem tools: `agent/tools/filesystem/*`
- In-container executor daemon: `kali_executor/executor_daemon.py`

### Agent execution path
- **LangGraph path**: backend handles `POST /api/tasks/{task_id}/chat` via `backend/services/langgraph_chat/*` and streams through the same hub.

## Repository map + canonical docs
Current architecture docs (helpful, but still validate with code):
- `docs/architecture/architecture.md`
- `docs/architecture/management-plane.md`
- `docs/architecture/data-plane.md`
- `docs/architecture/execution-plane.md`
- `docs/architecture/agent-architecture.md`
- `docs/architecture/langgraph-graph-architecture.md`

When docs disagree with code:
- Prefer the wired entrypoints (e.g. `backend/main.py`, router mounts, service constructors) over standalone modules.
- Use grep to find the call site that actually executes in production.

## Rules for changing code in this repo
### DRY + separation of concerns
- Don’t duplicate logic that already exists in:
  - `backend/services/` (orchestration)
  - `backend/config/` (feature flags)
  - `backend/services/runtime_provider/` (runtime placement/provider boundary)
  - `backend/services/runner_control/` (managed runner registry, jobs, and messages)
  - `backend/services/docker/` (local Docker implementation details)
  - `backend/config/workspace_config.py` (workspace layout)
  - `agent/tools/filesystem/_helpers.py` (workspace-safe path resolution)

### Read docstrings to understand scope

- Most files have a docstring entry that explains responsibility, scope and the purpose of the file in its first 15-20 lines. Before deciding to add to any file, read that docstring to determine if this is the correct file to add that function.

### Runtime provider boundary is non-negotiable
- Runtime side effects must go through `RuntimeOperationService` and `backend/services/runtime_provider/*`, unless you are implementing provider internals.
- Local Docker behavior belongs under the local provider / Docker services. Managed runner behavior belongs under runner-control and runner provider services.
- Graph and agent nodes should receive serializable runtime identity and use runtime/tool services; do not put DB sessions, backend service objects, SDK clients, or decrypted secrets into graph state/checkpoints.

### Workspace isolation is non-negotiable
- Task workspaces live under `agent/workspaces/task-<id>/...` for local host/runtime paths and mount into local containers at `/workspace`.
- Host/app-owned file access must stay task-local and use the existing safe resolvers.
- Container filesystem tools may use absolute in-container paths such as `/`, `/opt`, `/tmp`, and `/workspace` only through runtime transports; they must not become arbitrary host-path reads.

### Secrets and tokens
- Never log: API keys, JWTs, cookies, bearer tokens.
- Prefer masked markers like `<KEY_SET>` / `<NO_KEY>`.

## Setup commands (common)
- Install Python deps:
  - Create venv, activate, then: `pip install -r requirements-dev.txt`
- Install Node deps:
  - `npm install`
- Required env:
  - `DATABASE_URL` is required by `backend/database.py` (the process raises if missing).
    - Example PostgreSQL: `postgresql://user:pass@localhost:5432/drowai`
    - Example SQLite (local/dev): `sqlite:///./drowai.db`

## Start the local development stack
- Start backend, managed runner, and frontend:
  `python3 scripts/local_dev.py up`
- Stop the local stack:
  `python3 scripts/local_dev.py down`

## Testing instructions
Pick the smallest set that proves your change:
- Python:
  - `pytest backend/tests -k <pattern>`
  - `pytest tests -k <pattern>`
- TypeScript:
  - `npm run check` (tsc)
  - `npm run build` (vite + server bundle)

Streaming/schema changes:
- Keep backend + frontend packet/types in sync (search for existing generators/scripts before editing TS types by hand).

## Repository maintenance policies

The following files are normative repository policies and must be followed for
work in their scope:

- `CONTRIBUTING.md`: contribution, branch, pull request, review, merge, test,
  documentation, and changelog requirements.
- `MAINTAINING.md`: maintainer change control, repository configuration,
  triage, release readiness, and regression handling.
- `RELEASING.md`: semantic versioning, compatibility, release tags,
  changelog finalization, validation, and release procedure.
- `SECURITY.md`: supported versions, private vulnerability reporting,
  disclosure, and security-release handling.

Apply these rules when changing the repository:

- Normal implementation, fix, documentation, test, refactor, and maintenance
  pull requests must not change the product version or create a dated release
  section.
- Version history starts at `0.1.0`. For the initial release cycle, assigned
  metadata and the `CHANGELOG.md` target are `0.1.0`. For every release
  cycle, preserve agreement among `pyproject.toml`, `package.json`,
  `package-lock.json`, `backend/main.py`, and an assigned changelog target.
- Run `python3 scripts/check_version_consistency.py` after changing any version
  source or release target.
- Only a dedicated release change may finalize `Unreleased`, create a release
  tag, or publish a GitHub Release, and only when the user explicitly requests
  release execution.
- Repository settings described in `MAINTAINING.md` are policy requirements,
  not implicit authorization to mutate GitHub settings. Apply external setting
  changes only when explicitly requested.
- Update the affected policy in the same focused change when repository
  behavior, compatibility commitments, support scope, or release controls
  change.
- If a policy statement disagrees with wired code behavior, verify the active
  code path and correct the policy as part of the scoped change. Do not preserve
  a known contradiction.
- If a policy conflicts with this file, follow this file and report the
  inconsistency.

## Documentation and changelog policy

- Code in wired paths is the source of truth. Update docs only after verifying
  the implemented behavior.
- Update only the canonical document affected by the change:
  - `README.md` for project status, prerequisites, and first-run setup.
  - `env.example` and deployment/runbooks for configuration or operator changes.
  - `docs/architecture/*` only for changed boundaries, responsibilities,
    contracts, persistence, or important data flows.
  - `docs/testing/*` only for changed test strategy, ownership, or release gates.
- Prefer updating an existing document over creating or duplicating one.
- Remove stale statements instead of adding contradictory notes.
- Verify documented commands, paths, configuration names, and links.
- Never include secrets, credentials, private targets, or sensitive logs.

### Changelog

Update `CHANGELOG.md` under `[Unreleased]` only for meaningful user-,
contributor-, or operator-visible changes, including:

- features and meaningful fixes;
- public API, configuration, default, or deployment changes;
- breaking changes, deprecations, removals, or material security changes.

Do not add entries for internal refactors, tests, formatting, routine
documentation edits, temporary work, or reverted changes.

Entries must describe the observable outcome concisely. Do not include phases,
task numbers, file lists, test counts, internal implementation details, or
development workflow notes.

Add entries only after implementation and validation. Do not create a release
section, choose a version, or add a release date unless explicitly requested.

## PR / change hygiene
- Run the relevant tests (above) before handing back work.
- Keep diffs focused; don’t mix refactors with feature fixes.
- Don’t include secrets in logs, test fixtures, or docs.

## “Don’t get tricked by residual code”
This repo contains legacy/residual modules. Before assuming something is active:
- Confirm it is imported by a wired entrypoint (`backend/main.py`, router mount, service factory).
- If you find dead code, mention it; don’t delete it unless asked.
