# v0.12.15 — late-pass budget pacing (2026-08-05)

## The bug this fixes, with its victims

v0.12.13's late pass worked exactly as designed on its first live morning
(2026-08-05) — and the design had a hole. The daily budget of 10 was
**first-come-first-served**, and Wednesday was a heavy earnings morning:
the very first pass at 07:10 ET forwarded 10 items and consumed the entire
day's budget. Every later pre-market signal — roughly **140 of them
between 07:10 and 09:20 ET, including GOOG (twice), LLY, AZN, MRVL, SHOP,
RKLB, KEY, and FISV** — was journaled
`IGNORE — late-window daily budget exhausted`. A PLTR-style 08:30 story
would have been dropped again, one layer up from the blind window we just
closed. (Shadow evaluations still ran, but shadow can never place orders;
these signals lost their real at-the-open path.)

## What it does

Two changes to `a4_premarket/late.py`, both deterministic, still zero
model calls:

1. **Paced allowance.** Each pass may forward at most
   `min(late_pass_max, ceil(budget_left / passes_left))` candidates
   (`passes_left` counts 10-minute timer firings remaining before the
   open, including the current one). Spending exactly the allowance each
   pass leaves at least one budget slot for every remaining pass down to
   the last one before the open — **by construction, an early flood can
   no longer consume the whole day.** On a maximal flood morning the
   budget spends out to exactly `late_daily_max` with the final pre-open
   pass still holding a slot (pinned by a unit test).

2. **Defer, don't discard.** Candidates that lose a pass's ranking while
   daily budget remains are **deferred back to the queue** (attempt
   refunded — repeated deferral can never DLQ them) and compete again
   next pass. So every pass forwards the best of the *whole pending
   morning pool* by A1 priority, not just its own 10-minute cohort — a
   high-priority 08:30 story beats a mediocre 07:05 leftover every time.
   Items still deferred when the open arrives simply stay on
   `signal.overnight` and are ranked by tomorrow's 07:00 sheet like any
   other overnight item. `IGNORE` rows now appear only when the daily
   budget is truly exhausted — visible, never silent, same reason string
   as before.

Config: `sheet.late_daily_max` raised 10 → **24** (~24 × 40 s ≈ 16 min of
A2 time after the open — measured A2 throughput on 08-05 was ~40 s per
signal), new `sheet.late_pass_max: 4`.

Replaying 2026-08-05 under the new rules: the 07:10 flood forwards 2
(best-priority) instead of 10, and slots remain available at every pass
through 09:20+ — the whole morning competes.

## What it deliberately does NOT change

- The 07:00 sheet run (`service.py`): untouched, byte-identical.
- The open-handoff mechanics: same `enqueue_delayed` to
  `signal.analyst`, same priority 45, same dedup key (nothing can be
  forwarded twice), same `available_ts` = open + blackout.
- A2 and C3 remain the judges at live opening prices; no thresholds, gate
  rules, or risk checks touched.
- The timer and unit files: unchanged — no systemd work in this deploy.

## Files

REPLACED (3): `src/a4_premarket/late.py`, `config/a4.yaml`,
`tests/unit/test_a4_late.py`.
NEW (2): these patch notes, the deploy guide.
Plus a one-line pencil edit: `pyproject.toml` version → `0.12.15`.

No migration. No service restarts. No new systemd units — the late pass
is a oneshot; the already-installed `a4-late.timer` picks up the new code
on its next firing.

## Tests

Release set **57 green** (test_a4_late 16 — incl. the flood-morning
pacing invariant and passes_remaining boundaries — plus test_a4_premarket,
test_triage_router, test_a5_thematic unchanged).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.14
```
