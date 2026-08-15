# v0.13.0 — Short selling (shadow-first)

**One release, one switch.** The system can now trade bearish theses as
SHORT entries on every lane — news short-horizon, news long-horizon, and
the scanner (which now watches the top *losers* as well as gainers). It
ships with `shorting.mode: shadow`: every would-be short runs the FULL
pipeline — direction gate, borrow check, SSR check, sizing, stops, exit
policy — and then journals a `SHADOW_SHORT` decision plus a priced
counterfactual row instead of an order. C4 cannot see a short in shadow
mode by construction (the v0.12.11 EH-shadow guarantee, applied to the
whole short book). Going live later is a one-line config edit + restart,
not a release.

Evidence base: LONG_ONLY was the single most expensive veto measured
(2026-08-04: 7 blocked shorts, avg +1.3% foregone), and every LONG_ONLY
veto since v0.12.10 already has a counterfactual row with `max_down_pct` —
"the best case for the short the LONG_ONLY book didn't take."

## The one design idea

**One sign, one place.** Every directional formula in the system now
routes through `src/common/direction.py` (new): stop placement, R-units,
P&L, trailing, tighten-only comparisons, realization targets, marketable
exit pricing, heat. `r_unit` stays POSITIVE for both sides (a short's stop
is above entry, so `r_unit = stop − entry`), which means the schema CHECK
`r_unit > 0` and every "positive R = winning" convention in the journal,
dashboard, and reports survive unchanged.

## What's new, by component

- **Schema (migration 013):** `intents.side` gains `SELL_SHORT` (short
  entry) and `BUY_TO_COVER` (short exit); `positions.side`
  ('LONG'|'SHORT', default LONG backfills all history); exit layers gain
  `DIVIDEND` and `BORROW`; position events gain `BORROW_LOST`;
  `dash_positions` view shows the side. Rebuilt CHECKs follow the
  migration-009 full-union rule; `test_schema_vocab.py` enforces it.
- **C3 gate:** the three LONG_ONLY checks become one `direction_gate`.
  With shorting (or a lane) off, bearish theses still veto with the exact
  string `LONG_ONLY` — history stays comparable. With it on:
  `SHORT_UNAVAILABLE` (not easy-to-borrow at Alpaca — checked live via a
  new 30-min-TTL assets client, `src/common/assets.py`),
  `SSR_RESTRICTED` (down ≥10% from prior close, Reg SHO rule 201 — we veto
  rather than build uptick-compliant execution), or through to a MIRRORED
  confirmation: a down-thesis confirms by falling, is GATE_EXTENDED by
  having fallen, is PRICED_IN by gapping down. EH shadow prices short
  hypotheticals at the bid. Scanner structure checks invert (below-VWAP /
  lower-range = continuation for a short). All three fail CLOSED on
  missing evidence.
- **A3 risk:** side-aware sizing — short entries price at the bid minus
  buffer, stops go ABOVE entry, and shorts clip against margin buying
  power (`regt_buying_power`, refreshed by C4 reconciliation) instead of
  settled cash. Two new short-book caps on top of every existing one:
  `max_short_heat_pct` and `max_gross_short_notional_pct`.
  `open_risk_dollars` is side-aware — the long-only formula returned $0
  for any short, which would have silently defeated every heat cap. New
  `EX_DIVIDEND` hard gate (no dividend-calendar source yet, so it flags
  `DIVIDEND_UNKNOWN` for now — same D7 pattern earnings had pre-v0.10.0).
  **The shadow fork lives here:** a fully-sized short in shadow mode
  journals `SHADOW_SHORT` + a counterfactual row (`rule='shadow_short'`,
  `veto_reason='WOULD_TRADE'`, priced at the real computed limit — the
  existing 10-minute sweep prices outcomes automatically) and never
  enqueues the intent.
- **C4 execution (live mode):** `SELL_SHORT` maps to the broker's `sell`
  from flat; a short's catastrophe stop is a BUY stop-market ABOVE the
  market (GTC, never widened — rules 16/20 apply symmetrically); every
  exit layer is mirrored (L1 stop = bar HIGH touches it; L4 target below
  entry, bar LOW touches it; L2 trails from the LOW-water mark and
  "tighter" means LOWER; covers price at/over the ask). Reconciliation
  understands broker shorts (negative qty + side field, now normalized in
  the wrapper), adopts them side-correctly, journals a vanished short as a
  possible buy-in, and re-checks borrow status each pass — an open short
  that leaves the ETB list raises `BORROW_LOST` + DEGRADED health for the
  operator (rule 12: no auto-exit). Two new mirror invalidation
  predicates: `close_above_prenews`, `session_close_above_prior_high`.
- **Scanner:** fetches top losers (config `scanner.include_losers`), same
  one API call; the 4% move bar applies to magnitude; freshness for a
  loser measures from the day's LOW; LULD headroom uses the DOWN band.
- **A2 analyst:** prompts only — no schema change. `direction` is now the
  honest price call in both directions ("down" proposes a short);
  `magnitude_est: 0` remains the no-trade verdict (v0.12.9), so the old
  "down = reject" overload on the scanner lane is gone. Squeeze
  fingerprints are explicitly called out as dangerous in BOTH directions.
- **Everything that reads positions:** A6/A7/A8/A12/A13 prompts and P&L
  math, dashboard unrealized P&L, NAV snapshot — all side-aware.

## Safety rules delta

Rule 1 amends from "long-only" to: US equities only; no options; shorting
permitted ONLY in easy-to-borrow names, per-lane config-gated,
shadow-first; no leverage beyond Reg-T short margin. Rule 24's cash-account
containment is unchanged for the paper account (Alpaca paper accounts are
margin-capable); the live-money account structure is explicitly deferred
until paper shorts have a track record.

## What this release does NOT do

No locate API / hard-to-borrow shorting (`SHORT_UNAVAILABLE` rows will
measure what that would be worth). No uptick-compliant SSR execution (veto
instead). No dividend calendar yet (flagged, not vetoed). No EDGAR
ingestion widening. And in shadow mode, **no orders** — flipping to live is
the operator's explicit config decision, lane by lane if desired.

## Test evidence

Release set: **611 unit tests green** in the build sandbox (572 baseline +
39 new in `tests/unit/test_shorting.py` — the sign-flip proof matrix:
every exit layer parametrized LONG vs SHORT over identical geometry,
direction-gate matrix, short sizing geometry, FakeBroker short mechanics,
signed confirmation in all three gate branches). Pre-existing failures
(date-drift housekeeping list) unchanged. With shorting config absent or
off, every path is behavior-identical to v0.12.28 — the 87 existing gate
tests pass untouched.
