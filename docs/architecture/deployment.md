<!--
Purpose: document the current product deployment boundary and keep deployment
guidance aligned with the runner-only execution architecture.
-->

# Deployment Architecture

DrowAI product deployments use Management as the control plane and Runner as the
execution plane. Management owns auth, tenancy, setup, admission, task state,
assignment, runner-control jobs, and streaming. Runner owns task runtime side
effects: container lifecycle, shell and tool execution, workspace operations,
and runtime-side artifacts.

## Product Profiles

- Standalone profile: `deploy/compose/standalone.yml` starts Management,
  frontend, database, and a managed Runner on one host. Product task work still
  follows `Management -> Runner -> runtime`.
- Distributed control plane: `deploy/cloud/control-plane.yml` starts
  Management, frontend, and database only. Product task work requires a
  connected Runner Site.
- Runner Site package: `deploy/cloud/execution-site-package/compose.yml` starts
  the Runner using generated enrollment material.

## First-Run Setup Lifecycle

First-run setup is a Management-owned lifecycle, not a Runner responsibility.
`backend/main.py` mounts the setup router before authenticated application
routers and checks installation state during lifespan startup. For product
profiles, startup first validates the deployment/runtime policy and fails
closed active local-placement tasks. It then repairs legacy installs that have
users but no `platform_installations` singleton row, commits that repair, and
defers post-setup background services when setup is still required.

The browser shell enforces the same lifecycle. `client/src/App.tsx` wraps normal
routes in `SetupGate`, and `client/src/components/setup/SetupGate.tsx` redirects
to `/setup` while `/api/setup/status` reports `setup_required=true` and
`wizard_enabled=true`. The setup page posts the selected database, security,
display, network, and runner settings to `/api/setup/complete`; `/api/setup`
also exposes status, database validation, generated wizard secrets, debug-only
skip, and setup health endpoints.

Setup completion uses `SetupCompletionService` in
`backend/services/platform/setup_completion_service.py`. The service commits
durable provisioning state first: admin user credentials, default tenant
membership, admin display/session settings, optional execution site creation,
old setup-token revocation, a fresh install token, and installation status
`provisioning`. Only after that commit does it rotate PostgreSQL credentials
where applicable and publish generated runner enrollment. If generated artifact
publication fails, the installation is marked `failed` with a sanitized error
and can be retried. After successful artifact publication, the service marks
the singleton installation `complete` and the setup router reconciles
process-local background services.

## Generated Configuration and Secrets

Generated deployment configuration is centralized in
`backend/config/generated_config.py`, with `backend/config_bootstrap.py` as the
CLI wrapper used by Compose config initialization. The generated roots default
to `.drowai-local/{config,secrets}` for local Python launchers and
`/var/lib/drowai/{config,secrets}` for Docker profiles, unless
`DROWAI_CONFIG_DIR` or `DROWAI_SECRETS_DIR` is set.

Configuration precedence is explicit: process environment variables are the
highest-precedence override, then generated `backend.env`, then generated
secret files for `POSTGRES_PASSWORD`, `JWT_SECRET`, and `ENCRYPTION_KEY`.
`resolved_backend_env()` bootstraps missing generated files, reconciles the
requested deployment profile, and injects product runner policy for
`single_host` and `distributed`: `TASK_RUNTIME_PLACEMENT_MODE_DEFAULT=runner`,
`ENABLE_CLOUD_RUNNER_CONTROL=true`, `RUNNER_TOOL_COMMAND_ENABLED=true`, and the
configured data-plane object-store backend.

Database password rotation during setup updates the generated PostgreSQL secret
and rewrites generated `backend.env`, then reconfigures the backend database
binding for future sessions. Generated secret files are written with restrictive
file permissions by the generated-config helpers; setup responses do not expose
runner enrollment internals.

## Runner Enrollment Artifacts

Runner enrollment material is generated from durable runner-control state.
When setup creates a local Runner Site, it issues a one-time install token and
builds `enrollment.toml` with the Management URL, runner roots, TLS policy, and
runner labels. `GeneratedArtifactPublisher` keeps this file publication outside
the database transaction and uses atomic replacement so file existence is a
reliable signal for waiting launchers.

Artifact publication depends on the deployment profile. Standalone and local
development profiles use `FilesystemGeneratedArtifactPublisher` and publish
`enrollment.toml` under the generated config directory. Distributed Management
uses `NoopGeneratedArtifactPublisher` because it does not own a local Runner;
remote Runner Sites receive enrollment through runner-control APIs such as
`/api/runner-control/enrollments` and
`/api/runner-control/enrollments/package`. The Runner Site Compose package
copies packaged `config/enrollment.toml` into
`/var/lib/drowai/config/enrollment.toml` before starting the Runner, unless
durable runner credentials already exist.

Standalone installation starts the control plane first. The installer waits for
`/api/setup/status`, then waits until the backend-visible generated
`/var/lib/drowai/config/enrollment.toml` exists before starting the Runner. The
standalone Runner reads `DROWAI_RUNNER_CONFIG=/var/lib/drowai/config/enrollment.toml`
and uses stored runner credentials in `/var/lib/drowai/credentials` after
registration.

## Management Network Boundary

Product Compose profiles publish the frontend as the Management ingress. Nginx
proxies HTTP API and WebSocket traffic to the backend over `drowai-platform`;
the backend does not publish port 8000 on the host.

PostgreSQL, config bootstrap, and the backend share the separate `drowai-data`
network. Frontend and Runner services do not join that network. PostgreSQL host
publication defaults to `127.0.0.1:5432`; operators may set
`POSTGRES_BIND_ADDRESS` to a specific private Management address when trusted
remote database administration is required.

## Runtime Boundary

Product task creation, startup, terminal, tool, workspace, and artifact
operations must resolve to Runner placement. If no eligible connected Runner is
available, Management must fail closed with a structured readiness or admission
reason instead of selecting a Management-host runtime.

Management-host runtime access is reserved for explicit development, test, and
diagnostic utilities. It is not a product deployment path and must not be used as
a product fallback in standalone or distributed deployments.

## Background Services and Local Development

Background services are process-local backend services owned by
`backend/services/platform/background_services.py`: metrics, CVE sync,
report scheduling, terminal-session management, WebSocket cleanup, and
agent-log retention. `backend/main.py` starts them during lifespan only when
setup is not required. `/api/setup/complete` starts or repairs them immediately
after successful setup, and `/api/health` reports their non-secret status.

`scripts/local_dev.py` is the local parity launcher for the same managed-runner
control-channel architecture. It bootstraps generated local config, starts the
backend, starts the frontend when setup identity is missing, removes stale
local runner enrollment and credential files for fresh setup, waits for the
setup wizard to publish the local `enrollment.toml`, and then starts the Runner.
When a setup identity already exists, it can reuse stored runner identity or
bootstrap an install token before starting the Runner. This keeps local
development on the Runner placement path instead of reintroducing a
Management-host product runtime fallback.
