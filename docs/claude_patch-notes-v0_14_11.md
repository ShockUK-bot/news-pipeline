# Patch notes v0.14.11

A6 review verdicts now reach the exit ladder; thesis staleness clocks aligned. 2026-09-15. Previous tag: `v0.14.10`. Operator decision: no human in the loop, so options 2 and 3 from the RIOT investigation were built; option 1 (an operator exit command) was declined.

## Why

The nightly (A6) and morning (A8, re rendering A6) emails recommended exiting RIOT every night from 08-28, thirteen nights running, and nothing happened. A6 was recommendation only by design ("auto apply OFF, the operator decides"), no operator exit path existed, and the thesis lane has no time stop because "the thesis store IS the time stop": C11 only arms an exit when a thesis leaves ACTIVE. The RIOT thesis stayed ACTIVE because A5 expires a thesis after 6 weeks without thematic evidence, while A6 calls a position stale after 4 weeks without ticker news. Two clocks, no bridge.

## Change 1: the A6 to C11 bridge (`exit_on_review_verdicts`)

`src/c11_thesis/service.py`, nightly management pass (22:15 ET, after A6):
- New pure helpers `review_exit_due(verdicts, n)` and `management_action(status, verdicts, mcfg)`, and a store read `recent_nightly_verdicts(position_id, n)` (A6 nightly `POSITION_REVIEW` events only, not the EOD overnight check).
- For an open thesis position whose thesis is still ACTIVE: when the newest `n` A6 nightly verdicts all say `exit` with `thesis_intact: false`, C11 arms the same tighten only exit it already uses for EXPIRED and INVALIDATED theses (stop to 0.5 percent under the last mark; C4's L1 stop sells at the next open). Journaled as `STOP_TIGHTENED` (actor C11, reason "A6 exit verdict x3 (thesis_intact=false), exit armed") and a RISK decision `THESIS_REVIEW_EXIT` carrying the verdicts. Listed in the nightly digest as `REVIEW_EXIT`.
- A dead thesis still takes precedence; an `exit` verdict with `thesis_intact: true` (a trim or overextension call) does not count; a single `hold` inside the window resets the streak.
- `config/thesis_entry.yaml`: `management.exit_on_review_verdicts: 3` (0 disables).

Models still propose and code still disposes: one night's opinion never moves a stop; three consecutive nights of "thesis broken, exit" do, deterministically and journaled.

## Change 2: staleness clocks aligned

`config/a5.yaml`: `store.stale_weeks` 6 to 4, matching `config/a6.yaml` `review.stale_weeks: 4`. An ACTIVE thesis with no evidence for four weeks now expires (A5, 20:30 CT), which arms the existing dead thesis exit in C11 the same night. Pinned by a unit test.

## Tests

`tests/unit/test_v0_14_11.py`, 15 tests: the streak logic (arms at n, not at n minus 1, a hold resets, intact exits do not count, older holds do not matter, 0 disables, missing fields are not exit), `management_action` precedence, and the two config pins. Suite: 802 passed, 1 skipped.

Read only dry run against live data before commit: RIOT (position 7, thesis ACTIVE, verdicts exit x3) would be armed at 19.55 against a 19.65 mark; INVX (position 13) at 29.77 against 29.92; FRMI (position 33, verdicts hold x3) untouched.

## Services

None restarted. A5 (`a5-thematic.timer`, 20:30 CT) and C11 (`thesis-entry.timer`, 21:15 CT) are oneshots that read the working tree on each run, so the change is live from the first run after commit. First real effect: tonight's C11 run arms RIOT and INVX; C4 sells both at the 2026-09-16 open.

## Rollback

`git reset --hard v0.14.10` on `main`. No restart needed (oneshots). A stop that was already tightened stays tightened; to undo one, set `exit_policy.current_stop` back by hand with operator approval (a journal UPDATE).

## Deploy record

(filled in after the operator go and the first C11 run)
