<!--
Purpose: document the current distributed Management and Runner Site install
flow without exposing raw runner token mechanics as the primary product path.
-->

# Cloud Installation Guide

Cloud deployment uses two host roles:

- **Management host:** UI, API, and PostgreSQL from
  `deploy/cloud/control-plane.yml`.
- **Runner Site host:** packaged Runner from
  `deploy/cloud/execution-site-package/compose.yml`. Kali containers start on
  this host through the Runner-owned Docker socket.

Management does not mount the Docker socket and is not a product runtime
fallback. Product task execution requires a connected Runner.

## 1. Start Management

On the Management Linux host, from the full repo:

```bash
docker compose --project-directory . \
  -f deploy/cloud/control-plane.yml \
  up -d --build
```

Verify:

```bash
curl -sf http://localhost/api/health
```

The compose profile creates generated config/secrets volumes for stable
deployment values. Do not hand-author runner enrollment tokens or tenant ids in
`.env` for product setup.

## 2. Download Runner Site Package

Open the UI, complete setup if needed, then create a Runner Site from
**Settings -> Runner Sites**, then use **Download Package**. The downloaded
archive is already configured for that Runner Site. Copy it to the Runner Site
host.

## 3. Install Runner Site

On the Runner Site Linux host:

```bash
tar xzf drowai-runner-site-*.tar.gz
cd drowai-runner-site
docker compose up -d --build
```

The package contains `config/enrollment.toml`. The Runner exchanges that
enrollment material for durable credentials, connects to Management, and owns
Docker/runtime side effects for assigned tasks.

## 4. Verify Runtime Placement

Create a task in the UI after the Runner shows connected. Kali containers
should appear on the Runner Site host, not the Management host.

Management:

```bash
docker compose --project-directory . \
  -f deploy/cloud/control-plane.yml \
  logs backend --tail=200
```

Runner Site:

```bash
cd drowai-runner-site
docker compose --project-directory . -f compose.yml logs runner --tail=200
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| Task creation says no Runner is connected | Confirm the Runner service is healthy and can reach the Management URL in `config/enrollment.toml`. |
| Runner health check fails | Run `docker compose --project-directory . -f compose.yml logs runner --tail=200` on the Runner Site host. |
| Runtime image has the wrong architecture | Set `DROWAI_RUNTIME_IMAGE` on the Runner Site host before starting the package. |
| Management health is unavailable | Check `deploy/cloud/control-plane.yml` services with `docker compose --project-directory . -f deploy/cloud/control-plane.yml ps`. |

## Reference

- Control plane compose: [`deploy/cloud/control-plane.yml`](../../deploy/cloud/control-plane.yml)
- Runner Site package compose: [`deploy/cloud/execution-site-package/compose.yml`](../../deploy/cloud/execution-site-package/compose.yml)
- Deployment overview: [`deploy/README.md`](../../deploy/README.md)
- Architecture overview: [`docs/architecture/deployment.md`](../architecture/deployment.md)
