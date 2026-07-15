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
