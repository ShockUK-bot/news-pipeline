# v0.12.7 — C7 watchdog: dead services now send email (2026-08-02)

**Closes the hole exposed by the week of 2026-07-28:** the Spark rebooted
at 03:04 CDT, four never-enabled services (c1-ingestion, c2-dedup,
a1-triage, c4-exec) stayed down for FIVE DAYS, no trades were possible
from either lane, and the only signal was a vague ingestion note in the
daily emails. Separately, `pipeline-nav-snapshot` had failed every night
since v0.12.2 (its table's SQL shipped outside the numbered migration
sequence) and nothing said so. Until now the codebase had **no immediate
alert path at all** — nothing ever wrote an `ALERT` row to the outbox;
the dead-man switch blocks entries but only logs.

## What ships

A new **C7 watchdog**: a oneshot fired by `c7-watchdog.timer` every 5
minutes, deliberately independent of every service it watches (its only
dependencies are systemd and Postgres). Three check classes, all
configured in `config/watchdog.yaml`:

1. **Long-running services** (the ten pipeline services + dashboard +
   both llama slots): must be systemd-`active`, and must be `enabled` so
   they survive the next 3 AM reboot. A dead service is CRITICAL (with
   its down-since timestamp and a plain-English consequence, e.g.
   "EXECUTION ENGINE — no entries and no managed exits while down");
   an active-but-disabled one is a WARNING before it ever becomes an
   outage.
2. **Timer-driven jobs** (mailer, backup, NAV snapshot, queue prune,
   earnings calendar, A4–A8 report timers): the `.timer` must be active +
   enabled, and the job's **last run must not have failed** — this is the
   check that would have caught the NAV-snapshot bug on night one
   (`Result=exit-code`).
3. **Heartbeat staleness**: `journal.health` rows that are expected to
   refresh continuously (exec engine loop, deadman, marketdata, mailer,
   backup, earnings) must be younger than a per-component `max_age_min`.
   Catches "process up but wedged", which unit checks cannot see.
   `marketdata` is RTH-only (off-hours staleness is normal, v0.11.8
   lesson).

## Alerting — one email, not a siren

Findings become ONE plain-text email via the existing outbox → C5 mailer
path (the watchdog holds no SMTP credentials, rule 22; worst-case
latency ≈ watchdog 5 min + mailer 5 min). Anti-spam state rides in
`journal.control`:

- **NEW** finding set → email immediately (a changed set counts as new).
- Unchanged set → silence, then a **REPEAT** email every `realert_hours`
  (default 6) until fixed.
- All clear after findings → one **RECOVERED** email.

Subject line carries the counts and the worst unit:
`[watchdog] 2 critical / 1 warning — worst: c4-exec`.

## Dashboard truthfulness

For a service that is DOWN, the watchdog overwrites that component's
stale `journal.health` row to **DOWN** with a `watchdog:` detail (the
dead service cannot object). No more walls of frozen "OK". The watchdog
heartbeats its own `watchdog` health component every pass. Statuses stay
within the existing CHECK constraint (OK/DEGRADED/DOWN) — **no schema
change**.

## What it deliberately does NOT do

Restart anything. It reports; the operator acts. An auto-restarter that
fights a crash-looping service (or systemd's own `Restart=always`) is a
worse failure mode than a loud email. If Postgres itself is down the
watchdog can only log and exit non-zero — single-host system, no second
channel; that scenario remains covered by RUNBOOK §1 (broker-resident
catastrophe stops).

## Files

NEW (8 — nothing existing is modified):
`src/c7_watchdog/{__init__,service}.py`, `config/watchdog.yaml`,
`ops/systemd/{c7-watchdog.service,c7-watchdog.timer}`,
`tests/unit/test_watchdog.py`, `patch-notes-v0_12_7.md`,
`v0_12_7-deploy-guide.md`. Plus the pencil edit: `pyproject.toml` →
`0.12.7`. No schema change, no env change, no sudoers change (systemctl
status queries need no privileges).

## Tests

16 new unit tests, DB-free: systemctl output parsing + probe-error
survival; the finding matrix (dead service incl. the exact 07-28
signature of SERVICE_DOWN + NOT_ENABLED together, active-but-disabled,
failed oneshot last run, inactive timer, unit-not-found, stale heartbeat,
fresh heartbeat, rth_only gating off-hours, missing-row tolerance);
fingerprint stability under detail churn vs membership change; the full
alert lifecycle (NEW → quiet → REPEAT at 6h → RECOVERED, steady-healthy
silence, changed-set immediate re-alert); rendering (critical-first
ordering, worst-unit subject, REPEAT/RECOVERED variants).
`tests/unit/test_watchdog.py`: **16 passed** in the build environment.
Config verified to parse identically under PyYAML and the built-in
tiny-YAML fallback.

## Deploy-guide convention change (retroactive lesson, applies from now on)

Every future deploy guide that touches a systemd unit ends with a
**reboot-survival step**: `systemctl is-enabled` for every unit involved.
The 07-28 outage happened because units started manually at first deploy
were never enabled — that mistake now gets caught at deploy time (by the
guide) and within 5 minutes at runtime (by this watchdog).

## Rollback

`sudo systemctl disable --now c7-watchdog.timer` + `git checkout
v0.12.6`. The two control keys and the `watchdog` health row are inert
leftovers; nothing else changes.
