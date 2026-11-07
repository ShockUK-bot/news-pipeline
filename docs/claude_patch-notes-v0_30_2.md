# Patch notes v0.30.2 (2026-11-07): shadow lanes can actually journal; breakeven variants measured

## Fix: catalyst shadow lanes wrote nothing all week

v0.28.0 journals SHADOW rows to `journal.scanner_candidates`. That table has a check constraint on `status` that lists EMITTED, FILTERED, SUPPRESSED_NEWS, CAPPED and BREAKER only, and I did not see it when I built the lane (the constraint lines were filtered out of the schema listing I read). Every shadow insert failed from Tuesday 2026-11-03 to Friday 2026-11-06 (31 `catalyst shadow error` lines in `c10-scanner`, no other effect: the error is caught per pass, the main scan was never disturbed). Thursday alone had dozens of qualifying earnings reactions and Friday a dozen 15 percent gaps that the lanes should have measured.

`schema/migrations/023-scanner-candidates-shadow-status.sql` widens the constraint to include SHADOW. Additive, touches no rows. Needs the operator's yes to run; then `c10-scanner` restart is not strictly required (the insert simply starts succeeding) but is harmless.

## Measurement: breakeven ratchet variants

Thursday 2026-11-05 the scanner shorted TSLA at 10:03 ET and AVGO at 10:48 ET. Both were correct and both were closed by the breakeven ratchet (scalp_v1 `breakeven_at_R: 0.75`) within 9 and 20 minutes on a bounce, for +149 and -103, while the names fell another 2.9 and 5.7 percent into the close. TSLA's ratchet moved to breakeven six seconds after the fill, because the first bar's progress already read 0.95 R. `ops/tools/scalp_replay.py` gains two variants that A11 journals nightly for every scanner trade: `be1.5` (breakeven only from 1.5 R) and `nobe` (no breakeven ratchet; the trail still engages at 1 R). A9's existing variant rules will show whether the ratchet earns its keep. No live change.

Files: migration 023, `ops/tools/scalp_replay.py`, `tests/unit/test_v0_30_0.py`. Version 0.30.2. Suite 942 passed. Services: none to restart for the variants; `c10-scanner` optional after the migration.
