# v0.5.9 — SIP data feed, visible data starvation, RSS timestamp fix (2026-07-18)

Outcome of the July 16–17 no-trade review (project doc
`claude/no-trade-review-2026-07-16-17.md`): the system went 0-for-65 at the C3
gate, and the largest veto bucket (GATE_NO_CONFIRM) was untrustworthy — the
free IEX feed returned zero minute bars in the short since-news windows C3
evaluates, `avg_minute_volume()` silently returned None, and missing data was
auto-vetoed indistinguishably from genuine non-confirmation.

## Changes

1. **Alpaca feed is now configurable and defaults to SIP** (full consolidated
   market — requires the Algo Trader Plus subscription, active on this
   account since 2026-07-18). `ALPACA_FEED` in `/etc/pipeline/pipeline.env`:
   `sip` (default) or `iex` (free tier / emergency fallback). An invalid
   value fails loudly at startup. The feed in use is logged at C3 startup.
   — `src/common/marketdata.py`

2. **New veto code `MARKETDATA_MISSING`** — when `vol_mult` is None the
   intraday rule still vetoes (fail safe, principle 12), but journals it
   distinctly instead of as GATE_NO_CONFIRM, sets
   `journal.health.marketdata = DEGRADED`, and logs a WARNING. A starved
   data feed can never again masquerade as "the market didn't confirm."
   Additionally, every *successful* volume computation now refreshes the
   `marketdata` heartbeat, so that heartbeat is meaningful during market
   hours (previously it froze whenever no positions were open, keeping
   deadman in a permanent cosmetic DEGRADED).
   — `src/c3_gate/rules.py`, `src/c3_gate/service.py`

3. **RFC-822 RSS timestamps parse correctly** — globenewswire publishes
   `'Fri, 17 Jul 2026 22:30 GMT'` (no seconds), which `parse_ts` rejected,
   quarantining every item from that feed as BAD_TIMESTAMP. `parse_ts` now
   falls back to the email date parser for the RFC-822 family; naive
   (zone-less) timestamps are still rejected.
   — `src/common/clock.py`

## Files

REPLACED: `src/common/clock.py`, `src/common/marketdata.py`,
`src/c3_gate/rules.py`, `src/c3_gate/service.py`, `env.example`,
`tests/unit/test_normalize.py`, `tests/unit/test_analyst_gate.py`
NEW: `tests/unit/test_marketdata_feed.py`, `PATCH_NOTES_v0_5_9.md`,
`DEPLOY-v0_5_9.md`

No schema changes. No config-file (yaml) changes. Requires one new line in
`/etc/pipeline/pipeline.env` (`ALPACA_FEED=sip`) — covered in the deploy guide.

## Tests

Full suite green on PG16: 161 unit + 72 integration, including 9 new tests
(MARKETDATA_MISSING distinct veto, handoff path unaffected, RFC-822 parsing
with/without seconds, naive still rejected, feed selection/validation).

## Rollback

`ALPACA_FEED=iex` in pipeline.env + `git checkout v0.5.8` + restart services.
