# v0.12.1 — C10 momentum scanner, trading live (2026-07-23)

Second of the three scanner releases (design:
`claude/momentum-scanner-spec-v1_0.md`; v0.12.0 shipped the TA context +
origin plumbing). This release adds the scanner itself and everything the
scalp lane needs to trade **from day one at half size** per operator
decision: a new input, a new gate branch, a new exit profile with a hard
no-overnight rule, and layered anti-overtrading caps.

## What it does

**C10 (new service, `c10-scanner`)** polls the Alpaca Screener API every
60s from 09:50–15:15 ET for big gainers, measures each candidate off the
existing MarketData layer (move vs prev close, relative volume vs ADV(20)
pace, freshness vs high-of-day, spread, approximate LULD headroom, ETF and
earnings-day exclusion), cross-checks the news store ("does the news
pipeline already own this move?"), ranks survivors, and emits at most 2 per
scan / 6 per day / 1 per ticker per day as synthetic Tier-1 items
(`scanner:MU:2026-07-23`, headline "SCANNER: MU +6.2% on 4.0x relative
volume — no news match"). Every candidate SEEN is journaled to the new
`journal.scanner_candidates` (including rejects — A9's tuning evidence).
A dashboard-togglable `scanner_enabled` control and a daily circuit breaker
(3 losing scanner trades -> off until tomorrow) sit on top.

**Pipeline path** — same lanes, no shortcuts, `origin=scanner` end-to-end:
A1 (thin CODE handler — no 8B call; the metrics are the escalation case;
held names discarded) -> A2 with a scanner-specific prompt (inverse
question: "the market confirmed something — what, and is anything LEFT?";
magnitude = REMAINING move; minutes-scale `expected_move_window`; REJECT is
success; sympathy fan-out code-blocked) -> C3 **scanner gate branch**
("still tradeable?", all tape re-measured at gate time: SCANNER_STALE /
SCANNER_STRUCTURE / SCANNER_PARABOLIC / SCANNER_LIQUIDITY vetoes; LONG_ONLY
kept; credibility/confirmation/defer skipped — the move IS the signal) ->
A3 (profile `scalp_v1` by origin, **0.5× risk**, SCANNER_CONCURRENT cap at
2 open scanner positions, deterministic profile defaults — no discretion
call on this lane) -> C4 (stamps `positions.origin`).

**`scalp_v1` exit profile** — "capture part of the move, leave before it
mean-reverts, never hold overnight" made literal: stops/trails on ATR(14)
of completed **5-minute bars** (early-session fallback: daily ATR ÷ √78,
flagged `atr_5m_est` in the journal), breakeven at +0.75R, trail at +1.0R
(1.5× ATR-5m), scale-out 50% at 0.6× remaining-move estimate,
**minutes-based time stop** (<+0.5R after 60 min -> exit: a mover that
stops moving has no thesis), and **FORCE_FLAT at 15:50 ET** — an L6 code
rule in C4's engine loop that market-exits every `overnight_hold:
force_flat` position, runs even if every model is down, and is excluded
from the 15:45 overnight-decision pass (nothing to decide).

## Anti-overtrading, consolidated

top-2/scan → 6/day → 1/ticker/day → A2 REJECT authority → 4 scanner gate
vetoes → 2 concurrent scanner positions → 0.5× sizing inside the existing
SHORT-lane heat budget (no new heat bucket) → shared `max_trades_per_day`
(5) → daily loss circuit breaker → dashboard SCANNER ON/OFF toggle
(kill-token gated, audited). Expected steady state: 0–2 scanner trades on
a typical day; zero is a correct output.

## Dashboard

New **Momentum scanner** panel on LIVE: today's funnel (emitted / filtered /
news-owns-it / capped / losses), the last 8 candidates with reject reasons,
circuit-breaker + disabled banners, and the toggle. Pipeline-load panel maps
`signal.scanner` -> A1. Trades show SCAN chips via the v0.12.0 origin
column, which C4 now actually stamps.

## Files

NEW: `src/c10_scanner/{__init__,screener,rules,service}.py`,
`config/scanner.yaml`, `ops/systemd/c10-scanner.service`,
`schema/migrations/007-scanner.sql`,
`tests/unit/test_scanner.py`, `tests/unit/test_scalp_exits.py`,
`patch-notes-v0_12_1.md`, `v0_12_1-deploy-guide.md`
REPLACED: `config/exit_profiles.yaml`, `config/gate.yaml`,
`config/risk.yaml`, `src/a1_triage/service.py`,
`src/a2_analyst/{service,prompt,schema}.py`,
`src/c3_gate/{rules,service}.py`, `src/a3_risk/service.py`,
`src/c4_exec/{service,state,engine,exits}.py`,
`dashboard/{app.py,index.html}`

One DB migration (007: scanner_candidates table, scanner_enabled control,
FORCE_FLAT added to the exits / position_events vocabularies). One new
systemd unit. No new env lines, no model changes.

## Tests

47 new unit tests: the full candidate-filter reject matrix incl. null
fail-closed policy, scoring order, LULD approximation, scan window, the
scanner gate PASS/veto matrix with journaled numbers, minutes time stop
(fires stalled / holds progressing / needs minutes_open / sessions branch
untouched), atr_value-vs-daily-ATR trail geometry, A3 scalp
materialization + profile-by-origin, force_flat_pass (fires at 15:51,
waits at 15:47, ignores news positions) and the overnight-pass exclusion.
Suite: **320 passed** in this environment (same single pre-existing
env-sensitive failure as v0.12.0, unchanged on a clean checkout).
Verified live against PG16: migration 007; C10 `scan_once` end-to-end
(MU emitted + queued, WEAK rejected REL_VOLUME, TQQQ rejected
ETF_EXCLUDED, second scan deduped to zero); A1 handoff (TRIAGE/ESCALATE
row + origin=scanner preserved into `signal.analyst`).

## Rollback

`git checkout v0.12.0`, `sudo systemctl stop c10-scanner`, restart the six
touched services. Migration 007 can stay (additive). Or leave the code and
just flip SCANNER OFF on the dashboard — the input dies, news trading
untouched.
