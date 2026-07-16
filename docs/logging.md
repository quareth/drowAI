# Logging Guide

This guide describes the MVP file-based logging surfaces for local and
SaaS-ready support. The goal is a consistent place to look for each subsystem,
not a single bundled log collector.

## Principles

- Use standard Python module loggers: `logger = logging.getLogger(__name__)`.
- Include stable correlation fields when available: `tenant_id`, `task_id`,
  `user_id`, `runner_id`, `runtime_job_id`, `operation`, `status`, and
  `error_code`.
- Never log raw API keys, JWTs, cookies, bearer tokens, passwords, signed URLs,
  prompts, full tool output, or full command stdout/stderr.
- Tenant-visible logs must be read through tenant-authorized task APIs or
  provider task surfaces. Operator logs may include tenant/task identifiers for
  filtering, but are not a tenant-readable data surface.

## Where To Look

| Symptom | Primary logs |
| --- | --- |
| Backend does not start | `backend/log/backend.log` or the path configured by `LOG_FILE` |
| API or WebSocket request fails | Backend logs filtered by module, `tenant_id`, `task_id`, or request route |
| Task creation/startup fails | Backend task lifecycle logs and runtime-provider dispatch logs filtered by `task_id` |
| Local Docker runtime fails | Backend Docker service logs plus `/api/docker/docker-compose/logs/{task_id}` |
| Managed runner is disconnected | Runner log file filtered by `runner_id` and `tenant_id` |
| Runner task operation fails | Backend runner-control logs plus runner log file filtered by `runtime_job_id` and `task_id` |
| Agent reasoning or chat replay is wrong | Task reasoning history APIs, task `log.txt`, and `backend/log/langgraph_diagnostics.log` |
| Tool command produced unexpected output | Task workspace artifacts and bounded command result metadata, not backend operator logs |

## Backend Logs

Backend logging is configured at application startup from:

- `LOG_FILE`, default `backend/log/backend.log`
- `LOG_LEVEL`, default `INFO`
- `LOG_FORMAT`, default `%(asctime)s - %(name)s - %(levelname)s - %(message)s`
- `LOG_MAX_BYTES`, default `52428800` (50 MB)
- `LOG_BACKUP_COUNT`, default `5`
- `LOG_REDACTION_MAX_CHARS`, default `20000`

For local development:

```bash
python3 scripts/local_dev.py up
tail -f backend/log/backend.log
```

For compose deployments:

```bash
docker compose exec backend tail -f /app/backend/log/backend.log
```

Runtime-provider dispatch logs use event-like messages:

```text
runtime_provider.operation.start tenant_id=... task_id=... operation=... placement=... provider=...
runtime_provider.operation.end tenant_id=... task_id=... operation=... status=... error_code=...
```

## Runner Logs

Managed runner logs are emitted through standard Python logging. In cloud mode,
the default file is `<DROWAI_RUNNER_ROOT>/logs/runner.log`; otherwise it falls
back to `drowai_runner/log/runner.log`.

Runner logging is configured from:

- `DROWAI_RUNNER_LOG_FILE`, explicit file override
- `DROWAI_RUNNER_LOG_LEVEL`, default `INFO`
- `DROWAI_RUNNER_LOG_MAX_BYTES`, default `52428800` (50 MB)
- `DROWAI_RUNNER_LOG_BACKUP_COUNT`, default `5`

For compose deployments:

```bash
docker compose exec runner tail -f /var/lib/drowai/logs/runner.log
```

Useful runner events include:

```text
runner.cloud.start ...
runner.cloud.registration_succeeded ...
runner.cloud.channel_connected ...
runner.cloud.reconnect_scheduled ...
```

## Task-Visible Logs

Task-visible logs remain tenant-scoped and must be fetched through authorized
task paths:

- Reasoning/history: `/api/tasks/{task_id}/reasoning/history`
- Legacy system logs: `/api/tasks/{task_id}/logs`
- Runtime/container logs: `/api/docker/docker-compose/logs/{task_id}`
- Task workspace logs, when available through the runtime provider: `log.txt`
  and `error.log`

Do not expose backend operator logs directly to tenant users. For SaaS support,
operators should ship or mount backend/runner log files into the platform log
pipeline and filter by `tenant_id`, `task_id`,
`runner_id`, `runtime_job_id`, and `error_code`, then share only sanitized,
minimal excerpts.

## Retention

Backend and runner operator logs use size-based rotation. By default each keeps
the current file plus five rotated backups at 50 MB each. Task workspace logs
are task artifacts and follow task/workspace retention, not operator log
rotation. `backend/log/langgraph_diagnostics.log` has its own existing 50 MB
rotation with five backups.
