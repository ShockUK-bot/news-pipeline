# v0.12.9 — The analyst can say "no trade"; stale scanner signals expire (2026-08-02)

**Fixes the Sunday decision-tape flood of `ANALYST REJECT — model output
invalid after 2 attempts`.** Diagnosis showed the model was not failing —
it was being honest, and the contract gave honesty no legal shape.

## What actually happened

C10 kept scanning all through the Jul 28 outage week (it was one of the
reboot survivors), emitting momentum candidates into `signal.scanner` —
but A1, their consumer, was down, so ~a week of movers queued up. C10's
session-end expiry (spec §4) was never implemented, and A1 had no
staleness check. When A1 came back on 2026-08-02 at 12:05 it drained the
backlog straight into A2. The scanner prompt asks *"what is left of this
move?"* — and for a week-old mover the model answered correctly every
time: `direction: down, confidence: 0.1, magnitude_est: 0.0, "the move is
fully exhausted"`. But `ThesisOutput.magnitude_est` was `gt=0` — an
honest zero was a SCHEMA VIOLATION. So each dead signal burned two ~30s
model calls and was journaled as a model *failure* instead of a model
*judgment*. Fail-closed worked (nothing traded, nothing could have), but
the tape lied about why, and Monday's real signals would have paid the
same tax whenever the analyst honestly concluded "nothing left."

## The two fixes

**1. `magnitude_est: 0` is now the analyst's explicit no-trade verdict.**
The spec always said "REJECTING is success, not failure" — now the
contract means it:

- `schema.py`: `magnitude_est` bound relaxed `gt=0` → `ge=0` (upper bound
  0.5 unchanged; negatives still rejected).
- `service.py`: a valid thesis with magnitude exactly 0 journals
  `ANALYST REJECT` with `payload.no_trade=true` and the model's own
  priced-in assessment as the reason — one model call, no retries,
  nothing enqueued to the gate, no sympathy fan-out.
- `prompt.py`: both rule blocks (news + scanner) now teach it: *"set
  magnitude_est to 0 — that is the no-trade verdict, a valid and
  successful answer. Never invent a small positive number just to
  produce a thesis."* That last clause matters: the old contract's only
  escape from an honest 0 was a dishonest 0.01.

Telling the two REJECT flavors apart in the journal: judgment rejects
have `payload->>'no_trade' = 'true'` and reason `analyst no-trade: …`;
genuine model failures keep `model output invalid after N attempts`.

**2. A1 expires stale scanner signals before any model is consulted.**
New guard at the top of `handle_scanner`: if C10's `detected_ts` is older
than `router.scanner_expire_min` (config, default 15 — C3 already vetoes
anything >5 min after detection, so 15 is a generous ceiling), journal
`TRIAGE DISCARD` with `payload.expired=true` and the age, and stop.
Missing/unparseable timestamp fails open to the old behavior (C3's
`SCANNER_STALE` re-check still guards the trade itself). A dead mover now
costs one journal row instead of ~60 seconds of analyst time.

## Files

REPLACED (5): `src/a2_analyst/schema.py`, `src/a2_analyst/prompt.py`,
`src/a2_analyst/service.py`, `src/a1_triage/service.py`,
`config/a1.yaml` (adds `router.scanner_expire_min: 15`).
NEW (3): `tests/unit/test_no_trade_expiry.py`, `patch-notes-v0_12_9.md`,
`v0_12_9-deploy-guide.md`. Plus the pencil edit: `pyproject.toml` →
`0.12.9`. No schema migration. Two service restarts (`a1-triage`,
`a2-analyst`).

## Tests

9 new unit tests (DB-free): magnitude 0 now validates (the exact incident
shape), negatives and >0.5 still rejected, positive path unchanged, both
prompt blocks teach the 0 verdict; scanner age math (30 min, week-old
outage shape, fresh 2-min signal), and fail-open on missing/garbage/naive
timestamps. Neighboring suites re-run for regressions —
`test_analyst_gate`, `test_triage_router`, `test_scanner` — **89 passed**
total in the build environment.

## Housekeeping shipped with the deploy guide

One `chat.request` queue message stuck since 2026-07-16 is flushed
(marked done) — inert cruft found during the queue audit.

## Rollback

`git checkout v0.12.8` + restart `a1-triage` and `a2-analyst`. The
config key is ignored by old code; no schema to undo.
