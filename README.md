# TrendRadar Admin

Server-backed admin layer for [TrendRadar](https://github.com/sansan0/TrendRadar).

It reuses TrendRadar's official visual config editor, adds authenticated server-side save/run APIs, and writes directly to the same `config/` and `output/` volumes used by the main TrendRadar container.

## Features

- Token-protected admin UI on port `8081`.
- Loads the local official TrendRadar editor from `docs/index.html`.
- Saves real server files instead of browser-local storage:
  - `config/config.yaml`
  - `config/frequency_words.txt`
  - `config/timeline.yaml`
  - optional AI prompt/interest files.
- Validates YAML before writes.
- Creates timestamped backups under `config/backups/` before writes/restores.
- Can trigger a manual `python -m trendradar` run without restarting the main TrendRadar container.
- Removes official static-editor actions that are unsafe for server mode, such as loading remote defaults or copying local-only config.
- Synchronizes module navigation: clicking a config module number jumps both the YAML editor and the visual module card.

## Layout

```text
admin/admin_server.py   # Python HTTP admin backend
admin/admin_bridge.js   # Browser-side bridge injected into official editor
deploy/install.sh       # Installer for an existing TrendRadar directory
deploy/                 # Compose examples and env template
docs/deployment.md      # Full deployment guide
```

This repo does not vendor TrendRadar itself. Deploy it beside a TrendRadar checkout or release tree containing:

```text
config/
docs/index.html
docs/assets/...
output/
```

## Quick Deployment

For a normal install into an existing TrendRadar directory:

```bash
git clone https://github.com/tantanwu/Trendradar-admin.git /opt/trendradar-admin-src
cd /opt/trendradar-admin-src
sudo bash deploy/install.sh --trendradar-dir /opt/trendradar --image local/trendradar:6.10.0 --port 8081
```

The installer checks required TrendRadar files, copies the admin code into `/opt/trendradar/admin`, creates `/opt/trendradar/admin/.admin-token` if missing, writes `/opt/trendradar/docker-compose.admin.yml`, and starts `trendradar-admin`.

Full guide: [docs/deployment.md](docs/deployment.md).

## Required TrendRadar Files

This repository is not a full TrendRadar fork. It expects TrendRadar itself to provide:

```text
docs/index.html
docs/assets/...
config/config.yaml
config/frequency_words.txt
config/timeline.yaml
output/
```

If `docs/index.html` or `config/config.yaml` is missing, `deploy/install.sh` stops and tells you to install/fix TrendRadar first. It only creates files that should be machine-local, such as `admin/.admin-token` and `output/`.

## Manual Deployment

Create an admin token file outside git:

```bash
mkdir -p admin
openssl rand -base64 32 > admin/.admin-token
chmod 600 admin/.admin-token
```

Mount this repo's `admin/` directory into the TrendRadar image and mount the existing TrendRadar `config/`, `docs/`, and `output/` directories as shown in `deploy/docker-compose.admin.yml`.

Example service:

```bash
docker compose -f deploy/docker-compose.admin.yml up -d
```

Then open:

```text
http://<host>:8081/?token=<contents-of-admin/.admin-token>
```

The token is set as an `HttpOnly` cookie and subsequent API calls use that cookie.

## Security Notes

- Do not commit `.admin-token`, `.env`, generated configs, or output reports.
- Keep `8081` on a trusted network, VPN, or reverse proxy with HTTPS if exposed beyond LAN.
- The admin process needs write access to `config/` because it edits live TrendRadar configuration. The main TrendRadar service can keep `config/` read-only.
- If `TRENDRADAR_ADMIN_TOKEN` or `TRENDRADAR_ADMIN_TOKEN_FILE` is missing, the server fails closed and exits.

## Runtime Environment

Required environment variables for the admin service:

```text
TRENDRADAR_ADMIN_TOKEN_FILE=/app/admin/.admin-token
TRENDRADAR_CONFIG_DIR=/app/config
TRENDRADAR_OUTPUT_DIR=/app/output
TRENDRADAR_DOCS_DIR=/app/docs
TRENDRADAR_ADMIN_DIR=/app/admin
```

Useful optional variables:

```text
TRENDRADAR_ADMIN_HOST=0.0.0.0
TRENDRADAR_ADMIN_PORT=8081
TRENDRADAR_RUN_TIMEOUT=900
```

## Local Check

```bash
python3 -m py_compile admin/admin_server.py
```
