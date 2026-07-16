# DrowAI Deployment

Product deployment has two supported lanes:

- **Standalone:** one Linux host runs postgres, backend, frontend, and runner.
- **Distributed:** a Management host runs UI/API/DB, and Runner Site hosts run the packaged runner.

Local parity and development use [`scripts/local_dev.py`](../scripts/local_dev.py); it is not a customer deployment entrypoint.

## Prerequisites

- Linux host with Docker Engine and Docker Compose plugin.
- Permission to use `/var/run/docker.sock`.
- Runner state directory at `/var/lib/drowai`.

```bash
sudo mkdir -p /var/lib/drowai
sudo chown "$(id -u):$(id -g)" /var/lib/drowai
```

## Standalone

The canonical standalone entrypoint is the Compose profile:

```bash
docker compose --project-directory . \
  -f deploy/compose/standalone.yml \
  up -d --build
```

Then open the frontend and complete the setup wizard. Compose creates generated
config/secrets volumes for JWT signing, provider-credential encryption, and the
initial database bootstrap password; users do not create `.env` for deployment.
The default Standalone URL is `http://<server-address>` on HTTP port 80, matching
the Distributed Management default.

The frontend is the only published application service and proxies `/api` and
`/ws` to the backend over the internal platform network. PostgreSQL publishes
port 5432 on `127.0.0.1` by default. For database administration from a trusted
private network, set `POSTGRES_BIND_ADDRESS` to a specific private address on
the Management host and restrict that address with the host or network firewall.
Setting `POSTGRES_BIND_ADDRESS=0.0.0.0` is an explicit operator choice and must
not be used without equivalent ingress restrictions.

The compose profile still sets the internal backend value `DROWAI_DEPLOYMENT_PROFILE=single_host`; this is intentional compatibility with the application.

## Distributed

Start Management:

```bash
docker compose --project-directory . \
  -f deploy/cloud/control-plane.yml \
  up -d --build
```

The distributed Management profile uses the same boundary: clients reach the
backend through the frontend proxy, while PostgreSQL defaults to loopback and
can be bound explicitly to a trusted private management interface when remote
database administration is required.

Create a Runner Site in **Settings -> Runner Sites**. Management generates a
preconfigured Runner Site package:

Use **Download Package** for the Runner Site. The package is already
preconfigured with the canonical Management URL shown in the Runner Sites panel.
Only edit that URL when Runner hosts cannot reach the browser URL.

Install on the Runner Site host:

```bash
tar xzf drowai-runner-site-*.tar.gz
cd drowai-runner-site
docker compose up -d --build
```

The package contains `config/enrollment.toml`; the Runner exchanges it for durable credentials and then connects to Management. The `runner-config` Compose service remains available as an advanced fallback and writes `/var/lib/drowai/config/enrollment.toml`.

If a Runner host has already registered, stored credentials under
`/var/lib/drowai/credentials` take precedence over one-time enrollment material.
Remove `/var/lib/drowai` only when intentionally resetting that Runner host.

Full cloud runbook: [`docs/runbooks/cloud-installation.md`](../docs/runbooks/cloud-installation.md).

## Dev

```bash
python3 scripts/local_dev.py up
```

The launcher uses generated local secrets under `.drowai-local` when no explicit
secret overrides are present. Configure a separately running PostgreSQL database
through `DATABASE_URL`.

## Verification

```bash
python deploy/scripts/verify_profile.py
python deploy/scripts/verify_profile.py standalone
python deploy/scripts/verify_profile.py cloud
python scripts/package_execution_site.py --check
python scripts/build_runner_image.py --check
python scripts/build_runtime_image.py --check
```
