# Patch notes v0.25.0 (2026-09-22, 16:30 CT): C12 burst stream retired

Operator decision after three sessions on the realistic column (every cell negative after cost; table in `docs/claude_c12-retirement-and-reuse-2026-09-22.md`).

- `c12-burst` stopped and disabled (unit file kept in `ops/systemd/`, not enabled; do not start it). `config/watchdog.yaml`: service entry removed, `burst` added to `never_orphan` so the last heartbeat row is kept as history without alarms.
- Dashboard: BURST tab, its script and `/api/burst` removed (`dashboard/index.html`, `dashboard/app.py`). `c6-dashboard` restarted.
- A9: `rule_burst_go_live` removed from the active rules (function kept for the record).
- Kept: `src/c12_burst/` (pure book and detector, tested; the reuse plan for the scanner fast lane is in the retirement doc), `config/burst.yaml`, `ops/tools/burst_report.py`, `journal.burst_events` with all rows.
- `CLAUDE.md` services updated. Tests updated (`test_v0_16_1`, `test_v0_18_0`). Suite 901 passed.

Rollback: `sudo -n systemctl enable --now c12-burst`, restore the watchdog entry, `git reset --hard v0.24.2` and restart `c6-dashboard`.
