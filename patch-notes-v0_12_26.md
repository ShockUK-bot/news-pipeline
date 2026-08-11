# v0.12.26 — C11 thesis-entry lane: the store starts trading (2026-08-11)

## Why (the RIOT night)

2026-08-11 pre-market: RIOT — a beneficiary in the store's day-old "AI
Infrastructure Power" thesis — jumped +26% overnight on a thesis-confirming
catalyst (a >$1B/yr AI datacenter deal). The news lane could not, and
should not, have caught it: C3's open-handoff rule correctly vetoes a huge
gap as priced-in. **The only way to be in that move was to already own the
beneficiary because of the thesis.** Operator decision: build the long
lane's originator now, live on paper, evidence-first sizing discipline.

## What C11 does (pure code — no model call anywhere)

Oneshot nightly at **22:15 ET** (45 min after A5 updates the store):

**Entry planning.** ACTIVE, direction-up theses at confidence ≥ 0.5;
beneficiaries filtered by liquidity (price ≥ $3, 20d dollar volume ≥ $20M)
and the **don't-chase gate** (skip if last close > 10% above the
20-session mean OR > +15% in 5 sessions — a post-pop RIOT is SKIPPED by
design; the lane buys the quiet names before their RIOT moment). Caps: 2
new/day, 2 per thesis, 4 thesis positions total, never a ticker already
held or pending. Sizing: **0.25% risk** (half the news lane) over a
3×ATR(14) stop, min-viable-risk floor. Entries are delivered as
queue-delayed `exec.intent` messages — **available at the next session
open + 15 min, staggered 5 min apart** — with a limit at prev close
+2%: gaps beyond that simply never fill (C4 cancels at its fill timeout).
C4 runs its normal preflight/limit/fill/stop path; **zero changes to A3 or
C4 code.** Positions land with `origin='thesis'`, `profile='thesis_v1'`,
`horizon='LONG'`, and `thesis_decision_id` pointing at the A5 NEW_THESIS
row — full lineage for A6 review and A11 attribution.

**Management pass** (same run, over open thesis positions):

- **Dead thesis** (invalidated / realized / staleness-expired in the
  store): the position's live stop is tightened to 0.5% under the last
  mark and journaled (`STOP_TIGHTENED` position event + THESIS_DEAD_EXIT
  decision). The existing C4 L1 stop machinery exits it next session —
  the thesis store is the lane's time stop. Tighten-only, rerun-safe.
- **Earnings trim** (operator policy 2026-08-11: *"hold smaller size if
  there has been a gain and then look into repositioning after
  earnings"*): reports within ≤1 session AND unrealized ≥ +0.5R →
  `THESIS_TRIM_RECO` journaled + emailed. **v1 recommends, does not
  execute** — partial-exit automation needs C4 surgery and is deliberately
  a later release. Positions are half-size from birth precisely so this
  is survivable; `thesis_v1` sets `earnings_blackout_exit: false`.

One `RISK/C11/THESIS_PLAN` anchor per ET date (rerun = no-op); intent ids
are deterministic (`thesis-<id>-<ticker>-<session>`) so double-entry is
impossible even on a forced re-run. A plan digest email goes via the
normal outbox whenever there is anything to say.

## Files

NEW (10): `src/c11_thesis/{__init__,service}.py`,
`config/thesis_entry.yaml`, `schema/migrations/012-thesis-origin.sql`
(widens positions.origin CHECK to include 'thesis'),
`ops/systemd/thesis-entry.{service,timer}`,
`tests/unit/test_thesis_entry.py`,
`tests/integration/test_thesis_entry_flow.py`, both `.md` docs.

REPLACED (1): `config/exit_profiles.yaml` — adds `thesis_v1` (3×ATR stop,
4.5×ATR catastrophe, breakeven at 1R, weekly-ATR trail from 2R, no time
stop, realization review-flag, hold-through-earnings, default overnight
hold). Existing profiles byte-identical.

**11 changed files**, pyproject pencil edit to `0.12.26`. One additive
migration (012). One new timer. **No service restarts at all** — A3 never
reads thesis_v1, C4 consumes the intent queue continuously, C11 is a
oneshot.

## Tests

17 new. Unit (13): the don't-chase gate (quiet passes; the literal RIOT
+26% shape trips BOTH limits; short history refuses to trade; slow grinds
measured); liquidity floors; sizing (budget math, SNDK-style
stop-exceeds-budget, min-viable floor); the exit policy pinned against
**every key C4's `_open_position` and `evaluate_on_bar` actually read**
(including `atr_value`, re-materialization inputs, no force-flat, empty
machine_invalidations — thesis death is a store event, not a price
predicate); tighten-only stop math; deterministic intent ids; config pins
(risk ≤ news lane's, blackout ≥ 15 min, profile holds through earnings).

Integration (4, real PG16 through migration 012): plan → intents +
queue-delayed messages (C4's exact body shape asserted key-by-key) +
decisions + digest; same-date rerun no-op; the RIOT skip journaled with
numbers; dead-thesis stop tightened in place with event row, tighten-only
on rerun, and held-ticker exclusion.

Regression: the full money-path suites (risk/exec, guard, thesis store,
macro — 33 tests) pass unchanged. Sandbox full run: same pre-existing
failure set as v0.12.25 (the 8 Spark date-drift tests + 2 sandbox-only),
nothing new.

## Watch items

- A6's nightly review will now see thesis positions whose
  `thesis_decision_id` payload is A5-shaped (driver/beneficiaries) rather
  than A2-shaped (magnitude/window). It degrades to judging with what it
  has; if its notes look confused about thesis positions, that's a small
  A6-context follow-up, not a trading-path issue.
- A11 attribution: origin='thesis' needs its own bucket eventually (same
  note as scanner promotion).

## Rollback

```bash
sudo systemctl disable --now thesis-entry.timer
sudo -u trader git -C /opt/pipeline checkout v0.12.25
```

Migration 012 stays (additive). To also cancel any not-yet-executed
planned entries:

```bash
sudo -u postgres psql -d trading -c "UPDATE journal.intents SET status='CANCELLED' WHERE intent_id LIKE 'thesis-%' AND status='PENDING';"
sudo -u postgres psql -d trading -c "UPDATE queue.messages SET done_ts=now() WHERE queue_name='exec.intent' AND dedup_key LIKE 'thesis-%' AND done_ts IS NULL;"
```

Open thesis positions keep their stops and exit normally under C4.
