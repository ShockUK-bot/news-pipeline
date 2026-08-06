# v0.12.16 — quiet budget exhaustion (2026-08-06)

## What v0.12.15's first morning proved, and the one rough edge

First live morning of budget pacing (Thu 2026-08-06) worked exactly as
designed: 24 late candidates forwarded 2-per-pass from 07:10 ET, tapering
to 1-per-pass, with the final slot spent at 09:20 ET — ten minutes before
the open. The whole pre-market competed; nothing was starved. (Compare
Wednesday: all 10 slots gone at 07:10 ET, ~140 signals dropped.)

The rough edge: the moment the 24th slot was spent, the final pass took
the entire accumulated pool — every item that had competed all morning
and lost every ranking round, ~100 of them — and journaled each as
`IGNORE — late-window daily budget exhausted (24 forwarded)` in a single
minute. Honest, but it flooded the decision tape in one burst and read as
a malfunction. It also killed those items same-day, cutting off the
next-morning path the deferral design already promises.

## What it does

One behavioral change in `a4_premarket/late.py`: candidates that don't
make a pass's allowance are now **always deferred** back to the queue
(attempt refunded — repeated deferral can never DLQ them), including
after the daily budget is exhausted. While budget remains they compete
again next pass, exactly as in v0.12.15. Once it's gone — or once the
open arrives — they simply stay on `signal.overnight`, and **tomorrow's
07:00 sheet ranks and journals every one of them** like any other
overnight item (model-ranked top-K, `IGNORE below top-K cutoff`, or
`EXPIRED_BULK` at `max_age_hours`). Same honesty, delivered by the
component whose job is ranking the overnight pool — and no 100-row burst
on the tape.

Guard and thesis routing are untouched: even with the budget spent, every
pass still claims the pool and routes open-position items to
`signal.guard` and ticker-less material items to `signal.thesis`
immediately. Capital protection never waits for tomorrow.

The `late pass done` log line drops the `over_budget` counter (it can no
longer be nonzero) — `forwarded`, `deferred`, `allowance`, `passes_left`,
`budget_left` remain.

## What it deliberately does NOT change

- Pacing math, allowance formula, config values: untouched
  (`late_daily_max: 24`, `late_pass_max: 4`).
- The 07:00 sheet run, the open-handoff mechanics, dedup keys, A2/C3:
  untouched.
- `PREMARKET/IGNORE` rows still exist where they carry information — the
  sheet's below-top-K and bearish-single-name verdicts. What's gone is
  only the late pass's same-day exhaustion burst.

## Files

REPLACED (1): `src/a4_premarket/late.py`.
NEW (2): these patch notes, the deploy guide.
Plus a one-line pencil edit: `pyproject.toml` version → `0.12.16`.

No migration. No config changes. No service restarts. No systemd work.

## Tests

Release set **57 green** unchanged (test_a4_late's pacing invariants pin
the allowance math, which this release does not touch; the changed branch
is the DB-side disposition of unforwarded items).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.15
```
