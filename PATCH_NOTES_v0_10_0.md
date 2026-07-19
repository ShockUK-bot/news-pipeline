# v0.10.0 — Earnings-calendar source (2026-07-19)

Closes the Phase-4 D7 deferral "Earnings calendar (P1)": the blackout code
path has existed since Phase 4 but ran blind — every entry journaled
`EARNINGS_UNKNOWN` and was allowed, meaning a short-lane position could sit
through its own earnings print. This release feeds it real dates. **No gate
logic changes**: the veto in `a3_risk/sizing.py` (`EARNINGS_BLACKOUT` when
`earnings_next_sessions <= 1` for profiles with `earnings_blackout_exit`)
ships exactly as written and tested in Phase 4 — it just finally receives
non-None input.

## The source — one call a day for the whole market

`c1_ingestion/earnings.py`, fired by `earnings-calendar.timer` daily at
05:15 ET (before A4):

1. **Fetch:** Alpha Vantage `EARNINGS_CALENDAR` — ONE CSV request returns
   ~3 months of upcoming report dates for the entire US market. The free
   key allows 25 requests/day; this service makes exactly one. Provider is
   config-pluggable (`config/earnings.yaml`); the key comes ONLY from the
   environment (`ALPHAVANTAGE_KEY` in `/etc/pipeline/pipeline.env`, rule
   22 — never git). Alpha Vantage reports errors as HTTP-200 JSON; the
   parser detects a non-CSV body and treats it as a provider failure.
2. **Store:** upsert into `news.earnings_calendar` (migration 005; PK
   `(ticker, report_date)` — re-runs are no-ops that refresh `fetched_ts`);
   rows older than 7 days pruned.
3. **Journal + health:** one `SYSTEM/C1 EARNINGS_REFRESH` row with counts;
   `earnings` health component OK. Any failure (missing key, rate limit,
   network) → `EARNINGS_REFRESH_FAILED` row + DEGRADED health — and
   nothing downstream breaks: A3 keeps flagging `EARNINGS_UNKNOWN`,
   exactly the pre-v0.10.0 behavior.

## Conservative session math

The CSV carries no before/after-market timing, so `session` stays
`UNKNOWN` and the math assumes the worst: `earnings_next_sessions` counts
NYSE sessions in `(today, report_date]` — 0 = reports today (could be AMC
tonight), 1 = next session (could be BMO tomorrow, i.e. tonight's gap).
The existing veto at `<= 1` therefore covers both dangerous gaps without
knowing the hour. Weekend and holiday rolls use the real NYSE calendar
(pinned in tests, including the July-3-2026 observed holiday).

## Wiring (both defensive — errors degrade to the old behavior)

- `a3_risk/service.py::earnings_next_sessions` — the D7 stub becomes a
  live store lookup. Unknown ticker / empty table / any error → None →
  `EARNINGS_UNKNOWN` flag, as before.
- `a2_analyst/context.py` — the deferred `earnings_date` key goes live
  (plus a new `earnings_next_sessions` key), and the `thesis_matches`
  context key now reads the Phase-8 watchlist (the router fact went live
  in v0.9.0; the analyst's context pack now matches). `sector` and
  `short_interest` remain null-key deferred.

## Files

NEW (8): `schema/migrations/005-earnings-calendar.sql`
(`news.earnings_calendar`, additive), `src/c1_ingestion/earnings.py`,
`config/earnings.yaml`,
`ops/systemd/earnings-calendar.{service,timer}`,
`tests/unit/test_earnings_calendar.py`,
`tests/integration/test_earnings_flow.py`, `PATCH_NOTES_v0_10_0.md`,
`DEPLOY-v0_10_0.md` (9 with the deploy guide).

MODIFIED (2): `src/a3_risk/service.py` (stub → live lookup, 5 lines),
`src/a2_analyst/context.py` (deferred keys go live, defensive helpers).
Plus the pencil edit: `pyproject.toml` → `0.10.0`.

ENV (1, new requirement): `ALPHAVANTAGE_KEY=` line in
`/etc/pipeline/pipeline.env` (free key; the deploy guide walks through
claiming it). Without the key everything still runs — the refresh journals
FAILED/DEGRADED and A3 flags EARNINGS_UNKNOWN as before this release.

No new sudoers. One additive migration (005).

## Tests

9 new (6 unit + 3 integration). Unit: CSV parsing incl. the HTTP-200 JSON
error trap and skipped garbage rows; session math — report today/next/two
sessions, weekend roll, the 2026-07-03 observed-holiday roll, past-date
clamp. Integration (real PG16): refresh over an injected payload (upsert,
re-run idempotency, prune, journal row, health OK); lookups + the A3
service hook and A2 context helpers against seeded rows (next-session
report lands inside the blackout window; unknown ticker → None); provider
failure (FAILED row, DEGRADED health, wiped table degrades lookups to
None — the EARNINGS_UNKNOWN path).

Build environment (PG16, fresh DB, migrations 001→005): **357 passed**
(v0.9.0's 348 + 9). All pre-existing A2/A3 tests pass unchanged — the
degradation contract preserves old behavior when the table is empty.

## Rollback

`sudo systemctl disable --now earnings-calendar.timer` +
`git checkout v0.9.0`. Migration 005 is additive — leave it in place. A3
returns to flagging EARNINGS_UNKNOWN on every entry.

---

## System status after this deploy (2026-07-19)

Phases 1–8 + the earnings P1 source live. **Remaining build order:**
Phase 9 (A8 morning briefing — all inputs now exist: A4 sheet, A5
watchlist, A6 recommendations, earnings-blackout windows), Phase 11
(A11/A9/A10 + C9 replay). **Open config work:** §14 gate-threshold tuning
(needs this week's SIP veto data — weekend of 07-25), sector source
(sector-heat clip; now the last null P1 key with short_interest), A12
auto-execution and A6 auto-apply criteria (ledger evidence accumulating).
Model-watch window closes ~07-24.
