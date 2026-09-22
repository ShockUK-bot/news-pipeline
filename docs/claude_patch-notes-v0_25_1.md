# Patch notes v0.25.1 (2026-09-22, 16:45 CT): breaker made safe for an unattended month; heavy slot morning guard

Operator away for October 2026 with no access to the Spark.

## 1. Drawdown breaker

- **Bug fixed.** `src/c4_exec/breaker.py` `day_pnl` counted a short's unrealised P&L with the long sign (price minus entry times shares), so a winning short read as a loss. Two shorts were open when this was found; at the v0.16.0 scanner size a short 3.5 percent in our favour would have looked like a 1,000 dollar drawdown and could have tripped the breaker falsely, and the breaker was one way: only the operator could clear it. Now side aware, like the dashboard.
- **Auto reset, guarded** (`config/deadman.yaml` `c4.breaker_auto_reset: {enabled: true, max_trips_per_30d: 3}`). Every engine pass calls `maybe_auto_reset`: a trip clears at the first pass of a later session (Chicago date), audited `BREAKER_AUTO_RESET`, unless three trips already happened in 30 days, in which case it stays tripped for the operator and one `BREAKER_AUTO_RESET_REFUSED` audit row plus one email are written per day. `enabled: false` restores the one way breaker.
- **Emails.** A trip, an auto reset and a refusal each send an ALERT (HTML) through the outbox. Before this a trip was a log line and a dashboard badge only.
- `auto_reset_decision` is pure and tested (same session, next session, limit, late evening trip across the UTC date).

## 2. Heavy slot morning guard

`ops/systemd/llama-heavy-guard.service` and `.timer` (08:15 CT weekdays, runs as root, no credentials): stops `llama-heavy` if a nightly run left it up, logs either way. A5, A7 and A9 stop the slot themselves when they finish; this covers a run that died mid way. `config/watchdog.yaml` timers list updated.

## Tests and services

`tests/unit/test_v0_25_1.py`. Suite 904 passed. Live check of the corrected day P&L before restart. Restarted `c4-exec` after the close; guard timer installed and enabled, next run 2026-09-23 08:15 CT.

## Rollback

`breaker_auto_reset.enabled: false` and restart `c4-exec` (keeps the sign fix), or `git reset --hard v0.25.0` and restart `c4-exec`; `sudo -n systemctl disable --now llama-heavy-guard.timer`.
