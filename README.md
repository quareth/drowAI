# DrowAI

<p align="center">
  <img src="client/src/assets/drow-logo.png" alt="DrowAI logo" width="180">
</p>

DrowAI is an active pre-v1 AI agent platform for running task-isolated security
workflows through a web control plane, LangGraph-based agent orchestration, and
provider-backed Docker/Kali execution runtimes with general-purpose interactive
terminal access.

The project is public as work in progress. It is functional, but not a polished
v1 release: setup, deployment packaging, APIs, and documentation may still
change while the architecture is stabilized.

## Links

- [Website](https://www.drowai.com)
- [User Guide](https://www.drowai.com/user-guide)
- [Demos](https://www.drowai.com/videos)


## Why This Exists

DrowAI explores what AI-assisted software development, often called “vibe
coding,” can achieve when applied to a large and complex application. The
current version was built entirely through AI-assisted coding under the
direction and review of a cybersecurity engineer with ten years of professional
experience but no formal software-development background. The project is both a
working platform and an ongoing examination of the strengths and limits of this
approach.

**Project story:** For a personal account of how DrowAI began and how I used
AI-assisted development to build it, read
[the story behind DrowAI on Medium](https://medium.com/@alcangunes/i-used-ai-assisted-development-to-build-agentic-red-team-platform-drowai-9eadad2a5a55).

## What Is In The Repo

- **Backend control plane:** FastAPI app for auth, tenants, tasks, chat,
  setup, settings, reporting, runner control, and realtime WebSocket/SSE fanout.
- **Frontend:** React + TypeScript UI for operating tasks, streams, artifacts,
  terminals, reports, settings, and setup flows.
- **Agent runtime:** LangGraph-oriented agent and tool runtime modules under
  `agent/`.
- **Runtime provider layer:** provider-neutral backend boundary for local Docker
  runtimes and managed runner runtimes.
- **Managed runner:** `drowai_runner/` process that connects to the control
  plane and starts per-task Kali runtimes through the host Docker socket.
- **Kali executor:** in-container execution support under `kali_executor/`.
- **Deployment assets:** local parity launcher plus standalone and distributed
  Docker Compose profiles under `deploy/`.

Development updates currently live in the [changelog](CHANGELOG.md) and the
[GitHub issue tracker](https://github.com/quareth/drowAI/issues).

## Tooling Surface

DrowAI provides two overlapping execution surfaces:

- **Structured assessment tools:** a curated catalog of tools explicitly wired
  for model planning, parameter validation, normalized result parsing,
  artifacts, provenance, semantic evidence, and Knowledge integration. See the
  [LLM-Visible Toolset](docs/tooling/llm-visible-tools.md) and the broader
  [Complete Registered Toolset](docs/tooling/registered-toolset.md).
- **Interactive shell:** general-purpose terminal access inside the task's Kali
  runtime. Through the shell, the agent can invoke installed security tools,
  utilities, and custom scripts even when they do not have a dedicated DrowAI
  tool definition.

The structured catalog is not a command allowlist. Its purpose is to define
which tools have dedicated result integration. Assessment-shell runs can retain
verified output, artifacts, and provenance, but invoking a CLI through the shell
does not automatically give its output tool-specific parsing, normalized
Knowledge records, or dedicated visualization in the Knowledge tab.
Utility-shell output remains transient and is not retained as reusable
assessment evidence.

The roadmap will expand the structured catalog for mission-critical tools that
produce meaningful assessment information. It is not intended to add dedicated
Knowledge integration for every utility or binary available in Kali.

> **Interactive shell safety warning:** The agent can execute general-purpose
> commands and send traffic to any destination reachable from its Kali runtime.
> A task's `scope.md` is prompt guidance only; it is not a command, target, or
> network enforcement boundary. Container isolation limits the execution
> environment, but it does not prevent out-of-scope activity. Use DrowAI only
> against systems you own or are explicitly authorized to test, and apply
> firewall, VPN, egress, or lab-level restrictions when technical scope
> enforcement is required.

## Current Architecture

DrowAI is organized around three planes:

- **Management plane:** FastAPI routers, tenant context, task lifecycle,
  runner-control, runtime dispatch, setup, settings, and realtime gateways.
- **Data plane:** relational records, task workspaces, stream packets,
  artifacts, reports, knowledge, and evidence.
- **Execution plane:** task-local Docker/Kali runtimes selected through the
  runtime-provider contract, either local or managed-runner backed.

The architecture-specific Kali runtime images are pulled from
[`drowai/kali-pentesting` on Docker Hub](https://hub.docker.com/r/drowai/kali-pentesting),
where published tags and image metadata can be inspected before use. The image
build definitions are also available in [`runtime/image/`](runtime/image/).

For deeper architecture notes, start with:

- [Application Plane Architecture](docs/architecture/architecture.md)
- [Management Plane](docs/architecture/management-plane.md)
- [Data Plane](docs/architecture/data-plane.md)
- [Execution Plane](docs/architecture/execution-plane.md)
- [Runtime Provider Architecture](docs/architecture/runtime-provider.md)
- [Agent Architecture](docs/architecture/agent-architecture.md)
- [LangGraph Graph Architecture](docs/architecture/langgraph-graph-architecture.md)

## Local Development

The canonical contributor path starts the backend, managed runner, and frontend
through the same control-channel architecture used by single-host deployments.

Prerequisites:

- Python 3.11 or newer;
- Node.js 20.19 or newer with npm;
- PostgreSQL 15 or newer, running with the pgvector extension available;
- Docker Engine or Docker Desktop for the managed runner and task runtimes.

Install the application dependencies first:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm install
```

The generated local defaults target a database named `drowai` as `drowai_user`
on `localhost:5432`. On the first `up`, the launcher checks for that login role,
database, and pgvector extension. If any are missing, it shows the planned
administrative changes and asks before creating them. The launcher first tries
the current local PostgreSQL administrator identity; if that is unavailable,
the interactive flow asks for a PostgreSQL administrator username and password.
Administrator credentials are used only for the bootstrap connection and are
not stored.

You can run the same bootstrap explicitly before starting the stack:

```bash
python3 scripts/local_dev.py bootstrap-db
```

For a non-default or password-authenticated application database, set
`DATABASE_URL` in the shell or an optional root `.env` file before running the
bootstrap. Remote or separately administered PostgreSQL installations should
normally be provisioned by their operator. A one-time
`DROWAI_POSTGRES_ADMIN_URL` override is available when the bootstrap must use a
specific administrator connection; do not commit or retain that credential.

Start the local stack and accept the database bootstrap prompt when it appears:

```bash
python3 scripts/local_dev.py up
```

The launcher generates local configuration and secrets under `.drowai-local`.
A root `.env` file is read only for development overrides. After first-run
setup and sign-in, model-provider credentials are configured under
**Settings → API**. Reviewed affordable choices include OpenAI
`gpt-5.4-mini` and Mistral Small 4 (`mistral-small-latest`).

`requirements-dev.txt` includes `requirements.txt` plus contributor and test
dependencies. Production images install only `requirements.txt`.

The launcher can provision the local development database objects, but it does
not install or start PostgreSQL or install the pgvector server extension. It
applies migrations after database readiness succeeds. SQLite is not supported
as the primary application database for this path. The launcher is for
development and parity testing, not the production deployment entrypoint.

Useful URLs:

- Frontend: http://localhost:5000
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs

## Deployment Paths

DrowAI currently has two product deployment lanes:

- **Standalone:** one Linux host runs Postgres, backend, frontend, and the
  managed runner.
- **Distributed:** a control-plane host runs UI/API/DB, while execution-site
  hosts run packaged runners that connect back to the control plane.

Standalone manual compose:

```bash
docker compose --project-directory . \
  -f deploy/compose/standalone.yml \
  up -d --build
```

Distributed control plane:

```bash
docker compose --project-directory . \
  -f deploy/cloud/control-plane.yml \
  up -d --build
```

See [deploy/README.md](deploy/README.md) for the deployment-oriented commands.

## Project Status

DrowAI is not a finished product release. The current focus is:

- stabilizing the task-isolated runtime model;
- completing tool implementations and extending LLM-ready tooling across the
  registered tool catalog;
- polishing agent behavior, memory, and context engineering;
- optimizing token usage and increasing cache hit rates;
- making knowledge extraction, data provenance, artifacts, and reports complete,
  stable, and reliable across the tool surface.

## Security Notes

DrowAI runs security tooling and task runtimes. Treat it like infrastructure:

- do not expose local development instances directly to the internet;
- keep JWT secrets, encryption keys, model keys, cookies, and runner tokens out
  of logs and commits;
- keep runtime side effects behind the runtime-provider boundary;
- keep task workspaces and streams tenant/task scoped.

Please report suspected vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## Contributing

DrowAI welcomes focused fixes and improvements while the project is pre-v1.
See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and pull-request
guidance.

## License

Apache-2.0. See [LICENSE](LICENSE).
