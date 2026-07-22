# v0.11.12 — Marketdata heartbeat probe (dead-man flapping fix) (2026-07-22)

Found while tracing why the system's **first-ever gate PASS** (PSKY, 12:43 CT,
+1.9% on 7.68× volume, 4 minutes after the EU cleared the $110B
Paramount–WBD merger) produced no trade. The C3→A3 chain shows the PASS was
vetoed `BLOCK_ENTRIES` — the dead-man switch had blocked all entries.

## Symptom

`journalctl -u c4-exec` for 2026-07-22 shows the dead-man **flapping all
session**: `dead-man BLOCK_ENTRIES alerts=[('marketdata', 2.3–2.5)]` →
`unblock: heartbeats recovered` 1–10 minutes later → block again, dozens of
cycles from the 08:30 CT open until the 15:00 CT close. The system spent
most of the trading day with entries blocked, unblocking only in brief
windows right after gate activity.

The PSKY case: block engaged 12:34:32. C3 computed PSKY's volume at
12:43:09 — refreshing the very heartbeat the dead-man was waiting for — but
A3 asked at 12:43:14 and the dead-man's next pass didn't unblock until
12:43:32. The trade missed by **18 seconds**, and the signal that unblocked
the switch was the trade itself.

## Root cause

Since v0.5.9 the `marketdata` health row is refreshed only inside
`C3Service.handle()` after a successful volume computation — i.e. only when
a signal happens to reach the gate. `config/deadman.yaml` sets
`marketdata: {alert_min: 2, block_entries_min: 2}`. So any >2-minute lull in
gate traffic — a perfectly normal quiet news stretch — ages the row past the
block threshold and the dead-man blocks all entries. The heartbeat measured
**news flow**; the dead-man needs it to measure **data-provider liveness**.

(v0.11.8b had already fixed the off-hours flavor of this — the row aging all
night — by making the marketdata alert RTH-only. This is the in-session
flavor: the row also ages between signals *during* the session.)

## Fix

`src/c3_gate/service.py`: C3's consume loop (which already carries the 60s
`gate` heartbeat from v0.11.7) now also runs a **marketdata liveness probe**
every 60s, plus once at startup: `snapshot()` of a liquid reference symbol
(SPY) against the Alpaca data API. On a positive-price answer it refreshes
`marketdata` = OK (`probe ok (SPY)`).

**On failure it deliberately writes nothing.** The dead-man reads the row's
*age*, so writing a fresh DEGRADED row on failure would reset the age and
blind the exact monitor this heartbeat feeds. Silence is the alarm: a real
provider outage now ages the row past 2 minutes and blocks entries, exactly
as designed — and that block will finally be *meaningful*.

The v0.5.9 refresh-on-volume-computation is kept (harmless extra freshness),
as is the `MARKETDATA_MISSING` DEGRADED write on a mature empty window.

Two new optional `config/gate.yaml` keys, defaults built into the code so no
config change is required: `marketdata_probe_secs: 60` (`<=0` disables) and
`marketdata_probe_symbol: SPY`. The shipped `gate.yaml` documents them; all
other values unchanged.

**No trading logic, gate rules, thresholds, sizing, or exec behaviour is
changed.** The dead-man itself (`deadman.py`) and its thresholds are
untouched — with a 60s probe, the 2-minute fuse now only trips when the data
API genuinely fails to answer for 2 minutes.

## Changed files

- `src/c3_gate/service.py` (full replacement — probe + consume-loop wiring)
- `config/gate.yaml` (full replacement — two new documented keys, same values)
- `tests/unit/test_marketdata_probe.py` (new — 7 tests)

## Tests

New `tests/unit/test_marketdata_probe.py` (7 tests) pins the probe contract:
success writes exactly one OK row, provider errors and zero-price answers
write **nothing**, the probe never raises into the consume loop. Full unit
suite on the dev container: **257 passed** + these, with only the
long-standing environment-sensitive `test_triage_v047::test_confidence_required`
failure unchanged. `service.py` compiles.

## Deploy

Upload pack → tag v0.11.12 → pull → **restart `c3-gate` only**. See
`v0_11_12-deploy-guide.md`. Safe any time; c3-gate restart loses no messages
(queue-backed).

## Rollback

`git checkout v0.11.11` + `sudo systemctl restart c3-gate`. No DB/schema/
timer/sudoers changes to undo.

## State of the system after 2026-07-22 (context for future sessions)

- v0.11.10's gate DEFER is **confirmed live**: fast in-session signals defer
  ~1–3 min then get informed PASS/VETO decisions with populated vol_mult.
- Today's veto mix (68 gate rows) looks *healthy*: GATE_NO_CONFIRM vetoes all
  show genuinely flat tape (±0.2% on 0.2–1.2× vol); LONG_ONLY is the biggest
  bucket (bearish theses, long-only book — strategy choice, not a bug);
  CREDIBILITY catching single-source small-caps as designed.
- **First gate PASS in system history**: PSKY 12:43 CT (blocked by this bug).
- Thresholds in `gate.yaml` remain §14 placeholders; collect a clean week of
  veto-mix data with this fix live, then tune.
