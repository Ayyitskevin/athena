# Athena packaging (dogfood)

Single-process FastAPI + SQLite. No multi-container stack.

## User service (no sudo)

```bash
cd ~/ai-workspace/athena-claude
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

mkdir -p ~/opt/athena-data/attachments ~/opt/athena-data/backups
cp deploy/athena.env.example ~/opt/athena-data/athena.env
# edit ATHENA_DB and ATHENA_ATTACH_DIR to absolute paths under ~/opt/athena-data

cp deploy/athena-desk.user.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now athena-desk.service

curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
athena-doctor "$HOME/opt/athena-data/athena.db" --attach-dir "$HOME/opt/athena-data/attachments"
```

## Retained backups

```bash
# Snapshot + keep newest 14 matching files in the backup directory
athena-backup \
  "$HOME/opt/athena-data/athena.db" \
  "$HOME/opt/athena-data/backups/athena-$(date +%F).db" \
  --keep 14 \
  --retention-glob 'athena-*.db'

# Prune only (no new snapshot)
athena-backup-prune "$HOME/opt/athena-data/backups" --keep 14 --glob 'athena-*.db'
```

Optional off-host: rsync the `backups/` directory to another node after each snapshot.

## System unit (root)

See `athena.service` + `athena.env.example` for `/opt/athena` installs.

## Import preflight

```bash
athena-validate-source jira-project path/to/jira-export.json
athena-validate-source confluence-space path/to/confluence-export.json
```
