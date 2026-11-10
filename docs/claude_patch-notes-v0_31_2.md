# Patch notes v0.31.2 (2026-11-10): the news lane gate was dead since 2026-11-02

## What went wrong

The v0.27.0 (EDGAR only news longs) and v0.29.0 (dilution short lane) changes to `src/c3_gate/service.py` were applied by text replacement anchored on the news item lookup query. That query appears twice in the file, and both edits landed on the first occurrence, which is the extended hours shadow branch (`_handle_eh_shadow`), not the main news branch (`handle`). The main branch kept using `item_source`, `cclass` and `lane` that were never assigned there. From the 2026-11-02 15:05 CT restart until this fix, every in session news thesis reaching the gate raised `NameError: name 'lane' is not defined` and was dropped after five attempts: 272 messages (227 Benzinga, 25 halts, 19 EDGAR, 1 PR Newswire), 156 of them published in session. The gate's `message failed` error line ran 270 times without a watchdog alert because the service stayed up and its heartbeat fresh.

Consequences: the EDGAR long lane and the dilution short lane never actually ran; on Thursday's trend day the news shorts would have failed the same way. Unaffected: the scanner lane (its own branch, which produced every trade this week), the analyst pre-check, the shadow lanes (once migration 023 was in), trend day mode's scanner side, and the extended hours shadow, which is where the LONG_ONLY vetoes in the digests came from.

Why the tests passed: the v0.27.0 and v0.29.0 wiring tests checked that the strings existed in the file, not where. The new `tests/unit/test_v0_31_2.py` parses the file and checks, per function, that any name used is assigned in that function.

## Fix

`handle` now selects `published_ts, source, headline` and assigns the three names before use. No other change. The 272 dropped messages are stale and are not replayed. Version 0.31.2. Suite 949 passed.

## Services

`c3-gate` restart. Rollback: `git reset --hard v0.31.1` and restart `c3-gate` (which reinstates the bug; the real rollback is v0.26.4 for the whole week).

## Lesson for the working method

Text replacement edits must assert the anchor is unique (the later releases did; these two did not), and wiring tests must check location, not presence. An error line that repeats hundreds of times on a healthy looking service needs a watchdog rule: `message failed` counts per unit per hour are in the journal already.
