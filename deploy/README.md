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

### Backup timer (user, no sudo)

Add `ATHENA_BACKUP_DIR` and `ATHENA_BACKUP_KEEP` to `~/opt/athena-data/athena.env`, then:

```bash
cp deploy/athena-backup.user.service ~/.config/systemd/user/athena-backup.service
cp deploy/athena-backup.user.timer ~/.config/systemd/user/athena-backup.timer
systemctl --user daemon-reload
systemctl --user enable --now athena-backup.timer
systemctl --user list-timers athena-backup.timer
# fire once now to verify:
systemctl --user start athena-backup.service
```

Use `loginctl enable-linger "$USER"` if the timer must run while logged out.

### Backup timer (system / root)

```bash
sudo cp deploy/athena-backup.service deploy/athena-backup.timer /etc/systemd/system/
# ensure /etc/athena/athena.env has ATHENA_DB + ATHENA_BACKUP_DIR
sudo systemctl daemon-reload
sudo systemctl enable --now athena-backup.timer
```

## System unit (root)

See `athena.service` + `athena.env.example` for `/opt/athena` installs.

## Import preflight

```bash
athena-validate-source jira-project path/to/jira-export.json
athena-validate-source confluence-space path/to/confluence-export.json
# machine-readable + fail on mapping gaps:
athena-validate-source jira-project path/to/export.json --json --strict
```
