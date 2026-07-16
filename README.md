# DrowAI

<p align="center">
  <img src="client/src/assets/drow-logo.png" alt="DrowAI logo" width="180">
</p>

DrowAI is an active pre-v1 AI agent platform for running task-isolated security
workflows through a web control plane, LangGraph-based agent orchestration, and
Docker/Kali execution runtimes.

The project is public as work in progress. It is functional, but not a polished
v1 release: setup, deployment packaging, APIs, and documentation may still
change while the architecture is stabilized.

## Links

- [Website](https://www.drowai.com)
- [User Guide](https://www.drowai.com/user-guide)
- [Demos](https://www.drowai.com/videos)
- [The story behind DrowAI on Medium](https://medium.com/@alcangunes)

## Why This Exists

DrowAI explores what AI-assisted software development, often called “vibe
coding,” can achieve when applied to a large and complex application. The
current version was built entirely through AI-assisted coding under the
direction and review of a cybersecurity engineer with ten years of professional
experience but no formal software-development background. The project is both a
working platform and an ongoing examination of the strengths and limits of this
approach.

Development updates currently live in the [changelog](CHANGELOG.md) and the
[GitHub issue tracker](https://github.com/quareth/drowAI/issues).

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

## Tooling Surface

The implemented tool registry and the LLM-facing tool catalog are intentionally
not the same thing.

- **Current LLM-visible toolset:** tools completed for model planning and
  self-selection, including wired parsing, normalized result projection, and
  knowledge/evidence-layer integration. See
  [LLM-Visible Toolset](docs/tooling/llm-visible-tools.md).
- **Complete registered toolset:** all executable `BaseTool` classes discovered
  by the runtime registry. See [Complete Registered Toolset](docs/tooling/registered-toolset.md).

Only the completed tool subset is visible to the LLM. A tool may be implemented
and registered in code, but it should not be exposed for model self-selection
until its argument contract, output parsing, compact result projection,
artifact/provenance behavior, and knowledge/evidence hooks are wired well enough
for the agent to reason over the result reliably. The full registry still
matters: it shows the broader implemented tool surface and the backlog of tools
that can be promoted into the LLM-visible catalog as they reach that standard.

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
- PostgreSQL 15 or newer, running with a provisioned database and login role;
- Docker Engine or Docker Desktop for the managed runner and task runtimes.

Before the first run, create a PostgreSQL database and login role using your
normal PostgreSQL administration tools. The generated local defaults target a
database named `drowai` as `drowai_user` on `localhost:5432`. If your database
uses different connection details or password authentication, set
`DATABASE_URL` in the shell or in an optional root `.env` file.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
npm install

python3 scripts/local_production_cloud.py up
```

The launcher generates local configuration and secrets under `.drowai-local`.
A root `.env` file is read only for development overrides. Model-provider
credentials are configured through the setup UI.

`requirements-dev.txt` includes `requirements.txt` plus contributor and test
dependencies. Production images install only `requirements.txt`.

The launcher applies migrations to create or update the application schema,
but it does not install or start PostgreSQL or create its database and login
role. SQLite is not supported as the primary application database for this
path. The launcher is for development and parity testing, not the production
deployment entrypoint.

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
