# Patch notes v0.25.2 (2026-09-22, 21:15 CT): off site backups

- `ops/journal_extract.py` + `journal-extract.timer` (03:05 CT): 20 tables as gzipped CSV (about 8 MB) force pushed nightly as one commit to the private repo's `journal-extract` branch, built inside the main repo's object store so it uses the repo's own credentials. First push done 21:10 CT; heartbeat `journal_extract`.
- `ops/backup_offsite.sh` + `backup-offsite.timer` (02:50 CT): newest `pg_dump` to Google Drive via rclone (`~/.local/bin/rclone` v1.75.1, installed without root), 21 days kept on the remote. Until the operator authorises Drive (instructions in `docs/claude_backup-offsite-2026-09-22.md`) it writes `backup_offsite DEGRADED` and exits cleanly. Heartbeat `backup_offsite`.
- `config/watchdog.yaml`: both timers and heartbeats. `CLAUDE.md` support timers updated.
- No service restarted. Rollback: disable the two timers.
