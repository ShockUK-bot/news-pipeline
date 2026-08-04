# v0.12.11 — extended-hours SHADOW mode (2026-08-03)

Operator request: most market-moving news (earnings, FDA, guidance,
upgrades) drops outside regular hours; the news lane currently parks it in
signal.overnight and arrives at the open AFTER the gap — Monday's tape had
analyst rejects literally saying "already dropped 6% pre-market." Before
building live extended-hours execution (a 3–4 release project whose hard
part is exit safety — Alpaca accepts only limit day orders in EH and NO
STOP ORDERS work outside RTH), we measure the opportunity honestly.

## What shadow mode does

During the extended sessions (pre 4:00–9:30 ET, post 16:00–20:00 ET),
every escalated tickered news signal is ALSO sent to A2 immediately,
tagged `eh_shadow`. A2 produces a real thesis (same model, same prompt);
C3 then evaluates it with quote-based EH rules and journals ONE decision:

- `GATE | WOULD_TRADE` — fresh, credible, not already extended, and a
  real two-sided quote tighter than `eh_shadow.max_spread_bps`. The
  journaled numbers include the live bid/ask/spread and the hypothetical
  entry (the ask — what a marketable EH limit would pay).
- `GATE | VETO` with rule `eh_shadow` — LONG_ONLY / CREDIBILITY /
  GATE_WINDOW (older than `eh_shadow.window_min`) / GATE_EXTENDED /
  EH_LIQUIDITY (no usable quote or spread too wide).

**No order path exists on this branch — by construction, not by
configuration.** The shadow handler never enqueues signal.risk, so
A3/C4 never see these signals. Real money cannot move.

Every shadow decision (WOULD_TRADE and vetoes alike) also gets a
`journal.gate_counterfactuals` row (v0.12.10's table — no new migration).
The sweep learned to measure post-market rows into the NEXT session
(close-walk over weekends included), so after one-to-two weeks the table
answers with numbers: how many EH entries, at what spreads, with what
outcome distribution — versus what the same stories did by the open.

## What is deliberately unchanged

The signal.overnight copy and the whole A4 premarket lane run exactly as
before — shadow is an ADDITIONAL observation, not a reroute. RTH trading,
the v0.12.10 re-check window, and the scanner lane are untouched. Volume
confirmation (vol_mult/VWAP) is NOT used in EH — thin books make it
meaningless; liquidity is judged from the live quote, which is what a
real EH order would face.

## Wiring details worth knowing

- Router (`router/rules.py` rule 4): off-session + EH window + tickered
  escalate → extra `signal.analyst` route tagged `eh_shadow`. Toggle:
  `router.eh_shadow_enabled` in `config/a1.yaml`.
- Dedup-key safety: the shadow copies carry their own queue keys
  (`…:eh_shadow` on signal.analyst, `…:ehshadow` on signal.gate) so the
  REAL morning re-enqueue of the same item can never be silently dropped
  by the queue's ON CONFLICT dedup.
- A2 skips sympathy fan-out for eh_shadow origin (existing origin gate),
  so a shadow thesis can't spawn synthetic signals.
- New `common.clock.extended_session()` — 'pre' | 'post' | None, ET,
  weekday-coarse like `is_market_hours` (a holiday shadow evaluation just
  finds no quote and records EH_LIQUIDITY; nothing real is at stake).
- Expected extra load: single-digit A2 calls per EH session at current
  overnight escalate volumes; the box is idle then.

## Files

REPLACED (10): `src/common/clock.py`, `src/router/facts.py`,
`src/router/rules.py`, `src/a1_triage/service.py`,
`src/a2_analyst/service.py`, `src/c3_gate/rules.py`,
`src/c3_gate/service.py`, `src/c3_gate/counterfactual.py`,
`config/gate.yaml`, `config/a1.yaml`
NEW (3): `tests/unit/test_eh_shadow.py`, these patch notes, the deploy
guide

No schema changes (reuses migration 010's table; decisions.action is
unconstrained text). No env/sudoers/unit changes.

## Tests

77 green in the release set (test_eh_shadow + test_triage_router +
test_gate_recheck + test_gate_defer + test_analyst_gate), 11 of them new.
Full unit suite: 409 green.

## The decision this data feeds

If two weeks of `WOULD_TRADE` rows show real, capturable moves at sane
spreads → we green-light the live-EH build (phased: EH gate thresholds
from this data → limit-only entries with software stop monitoring →
broker stops placed at the open). If they show wide spreads and
already-gone moves → we saved a month of dangerous work.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.10
sudo systemctl restart a1-triage a2-analyst c3-gate
```

(Or just set `eh_shadow_enabled: false` in config/a1.yaml and restart
a1-triage — the lane goes quiet, everything else stays.)
