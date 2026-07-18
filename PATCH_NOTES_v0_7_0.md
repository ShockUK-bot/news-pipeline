# v0.7.0 — Phase 6: A7 EOD Trade Report + C5 Mailer (2026-07-18)

Implements Phase 6 of `trading-system-baseline` v0.5: the daily end-of-day
email report (A7) and the outbox mailer (C5). First production use of the
staged heavy model slot.

## What ships every trading day (~16:35 ET / 15:35 CT)

One plain-text email: trades opened/exited with trigger headlines and exit
layers, realized P&L today and by exit layer, open positions with unrealized
R and current stop/basis, A12 guard verdicts, veto mix, pipeline throughput
(ingested / escalated / suppressed), control flags, non-OK health
components, ingestion gaps. **Every number is computed by SQL in
`a7_report/facts.py` (baseline rule 5) — the model never does arithmetic.**
A short model-written narrative (summary + up to 5 notables,
grammar-constrained) sits on top; if no model is reachable the report ships
anyway with a "narrative unavailable" line. The email is never blocked by an
LLM. (Timing note: baseline said ~4:30 PM CT; shipped at 15:35 CT — right
after the C4 session-close pass, so the report is in your inbox an hour
sooner. Timer is config-on-disk if you'd rather move it.)

## Heavy-slot lifecycle (first real job for the 122B)

A7 resolves its narrative model in order: **heavy (:8084)** — used if
running; else started via `sudo systemctl start llama-heavy.service`
(new sudoers rule `ops/sudoers.d/a7-heavy`), waited for up to 7 minutes, and
**stopped again afterwards ONLY if A7 started it** (ownership rule, same as
deadman blocks — an operator-started heavy session is never killed);
else **analyst fallback (:8081)** with the existing A13 wake rule; else no
narrative. Probe-first always; commands come from `config/a7.yaml`, never
from model output. The decision row records which slot narrated
(`model_id` provenance, as with every agent).

## C5 mailer — the only holder of SMTP credentials

Oneshot every 5 minutes (`c5-mailer.timer`): sends `QUEUED` rows from the
Phase-1 `journal.outbox` table, marks `SENT`, retries failures with
attempt accounting, gives up to `FAILED` after 5 tries, heartbeats the
`mailer` health component (DEGRADED when unconfigured or when rows error
out). Credentials AND recipients live in `/etc/pipeline/mailer.env`
(root-owned 0600, injected by systemd into C5 only — no agent-readable
context, rule-22 discipline; an agent cannot address email anywhere because
recipients never come from the outbox row).

## Files

NEW (nothing existing is modified):
`src/a7_report/{__init__,facts,narrative,render,service}.py`,
`src/c5_mailer/{__init__,service}.py`, `config/a7.yaml`,
`schema/migrations/003-outbox.sql` (ADDITIVE: attempts / last_error /
decision_id columns on the existing `journal.outbox`),
`ops/systemd/{a7-eod.service,a7-eod.timer,c5-mailer.service,c5-mailer.timer}`,
`ops/sudoers.d/a7-heavy`, `tests/unit/test_a7_c5.py`,
`tests/integration/test_report_flow.py`, `PATCH_NOTES_v0_7_0.md`,
`DEPLOY-v0_7_0.md`.

Plus the pencil edit: `pyproject.toml` version → `0.7.0`.

## Behavior notes

- Idempotent per session date: a re-fired timer or manual re-run cannot
  double-send (existing REPORT decision for the date → no-op).
- Holidays: the timer fires Mon–Fri; the in-code NYSE calendar check
  journals `SKIPPED_NO_SESSION` and sends nothing.
- Unconfigured mailer is safe: reports queue in the outbox, health shows
  `mailer DEGRADED`, nothing is lost; they send on the first pass after
  `mailer.env` exists.

## Tests

19 new (11 unit + 8 integration): narrative contract + grammar-safety pins;
renderer determinism on empty and busy days (subjects, negative P&L, flags);
slot-resolution order incl. probe-first no-start and the stop-only-if-we-
started ownership rule; fact-sheet SQL against seeded journal data;
report-without-narrative on invalid model output; same-day idempotency;
mailer QUEUED→SENT, retry→FAILED accounting, unconfigured-safe. Full suite
green on PG16 in the build environment (266); expect the Spark's v0.6.0
count + 19 (305).

## Rollback

`sudo systemctl disable --now a7-eod.timer c5-mailer.timer` +
`git checkout v0.6.0`. Migration 003 is additive — leave it in place.
