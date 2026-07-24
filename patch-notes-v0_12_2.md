# v0.12.2 — promotion rule, MAX TRADES button, Performance tab (2026-07-24)

Third scanner-family release (spec: `claude/momentum-scanner-spec-v1_0.md`;
scope: `claude/v0_12_2-scope.md`, operator-approved). No new services, no
config-value behavior changes to the news lane.

## 1. Scanner-trade promotion — "we were first"

When the scanner enters on pure price action and the CAUSAL news prints
afterward, force-flatting at 15:50 throws away the highest-edge trades.
Now:

- **A12 verdicts the evidence.** The guard's output gains
  `news_confirms_move` (default false; only meaningful on scanner-origin
  positions — position packs now carry `origin`). TRUE means "this item is
  plausibly THE story that caused the move we're riding". Cross-field rules
  enforced in code: TRUE requires `thesis_intact=true` and contradicts
  `recommended_action=exit`. The guard's action vocabulary stays
  risk-reducing-only — promotion is deliberately NOT a guard action.
- **C4 code executes.** A new `promotion_pass` in the engine loop (runs
  before the force-flat check, so a late confirm rescues the position)
  finds OPEN `origin='scanner'` positions with a confirming GUARD verdict
  and no `promoted_ts`, and graduates the exit policy via the pure
  `promoted_policy()` transform: `overnight_hold` force_flat →
  `eod_rule_v1` (the normal 15:45 D1 decision applies), minutes time stop →
  2-session window, trail/breakeven → short_term_v1 geometry on the DAILY
  ATR basis. **Never changed:** current stop / stop basis / hwm
  (tighten-only survives promotion), the broker-resident catastrophe stop
  (never widened), realization state, invalidation watch-lists, earnings
  blackout. One-way, once, idempotent (`promoted_ts`), journaled as a
  `PROMOTED` position_event carrying the confirming decision_id.
  `positions.profile` updates to `short_term_v1`; `origin` stays
  `scanner` (A11 will bucket promoted trades separately later).
- Config gate: `c4.promotion_enabled: true` in `deadman.yaml`.

## 2. MAX TRADES button (C6 header)

The `/api/max-trades` endpoint and A3's control-table override have existed
since dashboard spec v1.3 — the button never made it into the page. Now in
the header next to CAPITAL (kill-token gated, audited like the others), and
the prompt shows the current override. Set it high for the data-gathering
phase; A3 picks the new value up on the next signal, no restart.

## 3. PERFORMANCE tab (C6)

Portfolio total % change vs SPY and QQQ since the first trade, from the
existing `/api/performance` endpoint (fed nightly by
`pipeline-nav-snapshot.timer`). Line chart with crosshair + tooltip,
legend, direct end labels (collision-dodging), latest-value stat row, and
an expandable data table. Series colors were validated for colorblind
safety and contrast against the console's dark surface (six-checks
validator; the one floor-band pair is covered by the mandatory labels).
Rendered and screenshot-verified with synthetic NAV data. Shows an honest
"no NAV history yet" note until the nightly snapshot has rows — the deploy
guide includes a check that the timer is actually enabled.

## Files

NEW: `schema/migrations/008-promotion.sql`, `tests/unit/test_promotion.py`,
`patch-notes-v0_12_2.md`, `v0_12_2-deploy-guide.md`
REPLACED: `src/a12_guard/schema.py`, `src/a12_guard/prompt.py`,
`src/a12_guard/service.py`, `src/a12_guard/context.py`,
`src/c4_exec/engine.py`, `src/c4_exec/service.py`,
`config/deadman.yaml`, `dashboard/index.html`

One additive migration (008: `PROMOTED` event type). No new env lines, no
new services.

## Tests

9 new unit tests (`test_promotion.py`): schema cross-field matrix incl.
pre-v0.12.2 output compatibility, the promoted_policy transform (what
changes vs the risk state that must never change, input not mutated,
idempotency marker), and force-flat ignoring promoted positions. Suite:
**329 passed** (same single pre-existing env-sensitive failure).
Live-verified against PG16: migration 008; full promotion round-trip
(scanner position + confirming GUARD verdict → promotion_pass promotes
once, second pass idempotent, position row shows short_term_v1 /
eod_rule_v1 / 2_sessions / no force-flat, PROMOTED event with decision
lineage). Dashboard rendered headless and screenshot-inspected: LIVE
(scanner panel, MAX TRADES button) and PERFORMANCE (chart, tooltip,
dodged end labels, data table).

## Rollback

`git checkout v0.12.1b` (or `v0.12.1` if you re-tag) + restart
`a12-guard c4-exec c6-dashboard`. Migration 008 can stay (additive).
To disable only promotion: `promotion_enabled: false` in
`config/deadman.yaml` + restart c4-exec.
