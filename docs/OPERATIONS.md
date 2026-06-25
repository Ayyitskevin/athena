# Athena Operations Runbook

Athena is a local-first FastAPI service backed by one SQLite database. This
runbook covers the minimum safe path for running it as a long-lived service,
checking health, and backing up or restoring data.

## Runtime Model

- Run Athena as a dedicated unprivileged user.
- Bind Uvicorn to `127.0.0.1` and put any external access behind a reverse proxy
  or tailnet ingress.
- Keep the SQLite file and its parent directory writable by the service user.
- Keep `.env` and database files out of git. `.gitignore` already excludes them.
- Public internet exposure is a separate security decision; the default posture
  is local or tailnet-only.

## Environment

Start from [.env.example](../.env.example):

```bash
cp .env.example .env
```

Important variables:

- `ATHENA_DB`: SQLite file path.
- `ATHENA_TRUST_ACTOR_HEADER`: leave `0` unless you are on a trusted local box.
- `ATHENA_COOKIE_SECURE`: set `1` when served over HTTPS.
- `ATHENA_SESSION_TTL_DAYS`: browser session lifetime.
- `ATHENA_MAX_REQUEST_BODY_BYTES`: request body cap, default 1 MiB.

## First Boot

Install and run locally:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
ATHENA_DB=/var/lib/athena/athena.db .venv/bin/uvicorn athena.main:app --host 127.0.0.1 --port 8000
```

On startup, Athena runs forward-only SQLite migrations. The first user can be
created through the normal `/users` bootstrap path. After that, user creation and
token management require authentication.

## systemd Example

Adjust paths, user, and port for the host:

```ini
[Unit]
Description=Athena project management and knowledge base
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=athena
Group=athena
WorkingDirectory=/opt/athena
EnvironmentFile=/opt/athena/.env
ExecStart=/opt/athena/.venv/bin/uvicorn athena.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/athena

[Install]
WantedBy=multi-user.target
```

Useful commands:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now athena
sudo systemctl status athena
journalctl -u athena -f
```

## Health Checks

- `/healthz`: cheap liveness check. Does not touch SQLite.
- `/readyz`: readiness check. Verifies SQLite is reachable and migrations exist.

Examples:

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

## Backup

Use SQLite's online backup command so WAL-mode databases are copied safely while
Athena is running:

```bash
DB=/var/lib/athena/athena.db
OUT=/var/backups/athena/athena-$(date -u +%Y%m%dT%H%M%SZ).db
mkdir -p "$(dirname "$OUT")"
sqlite3 "$DB" ".backup '$OUT'"
sqlite3 "$OUT" "PRAGMA integrity_check;"
```

Keep several recent backups and periodically test restore on a separate path.

## Restore

Stop Athena, preserve the current database, then copy in the verified backup:

```bash
sudo systemctl stop athena
DB=/var/lib/athena/athena.db
sudo cp "$DB" "$DB.pre-restore.$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -o athena -g athena -m 0640 /var/backups/athena/athena-good.db "$DB"
sudo systemctl start athena
curl -fsS http://127.0.0.1:8000/readyz
```

Do not restore over a running database.

## Deploy Checklist

1. Confirm the branch is merged and the worktree is clean.
2. Pull the latest code on the host.
3. Install dependencies: `.venv/bin/pip install -e ".[dev]"`.
4. Run `ruff check .` and `pytest -q`.
5. Back up the current SQLite DB.
6. Restart Athena.
7. Confirm `/healthz` and `/readyz`.
8. Check the journal for startup or migration errors.

## Troubleshooting

- `readyz` returns 503: inspect the SQLite path, permissions, and migration table.
- Login works locally but not behind HTTPS: confirm `ATHENA_COOKIE_SECURE=1` only
  when requests reach the browser as HTTPS.
- Writes fail with 413: raise `ATHENA_MAX_REQUEST_BODY_BYTES` or enforce a larger
  limit at the reverse proxy.
- Header-based auth fails: `ATHENA_TRUST_ACTOR_HEADER` defaults off by design.
