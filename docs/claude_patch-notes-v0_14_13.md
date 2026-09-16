# Patch notes v0.14.13

The thesis store gets real input. 2026-09-15. Previous tag: `v0.14.12`.

## Why

No thesis has been created since the five seeded on 08-10. Investigation (read only, 09-15 evening):
- Router rule 3 sends only material items with **no mappable ticker** to the thesis lane (`signal.thesis`). Every nightly A5 input was therefore ticker less (31 of 31 last week), and a new thesis must name beneficiary tickers, so the nightly lane could not seed one by construction. A5's own digests said it nightly: "isolated corporate events lacking explicit US equity tickers".
- The only ticker bearing input was the Sunday deep pass (80 escalated items from the week), and with 4 ACTIVE theses against a floor of 3 the prompt was in its conservative "new theses are rare" mode. Three Sunday passes, zero proposals. No validation rejects, no errors.

## Changes

1. **A5 deep every night** (`config/a5.yaml` `lane.force_deep: true`): the wide, ticker bearing read of the week's escalations runs nightly. Cost about 5 minutes on the heavy slot per night instead of 2.
2. **A5 seeding mode until the store holds 6** (`store.bootstrap_min_theses` 3 to 6; `bootstrap_target` 5 to 6, the schema's per night cap). With 4 active the prompt flips to "seed the store; zero is a failure" from tomorrow's run and reverts on its own at 6.
3. **Router rule 5** (`src/router/rules.py`, `src/a1_triage/service.py`, `config/a1.yaml` `router.thesis_copy_min_score: 15`): a material, ticker bearing signal with `priority_score` at or above 15 also gets a copy on `signal.thesis` tagged `origin=thesis_copy` with its own dedup key; queue priority `100 - score` so the best claim first. 15 is the tier 1 high urgency band: about 33 of 200 escalations a day on the September sample. A5 reads the top 60 per night; the rest bulk expire after a week. The analyst and overnight routes are untouched; rule 3 is untouched. Removing the key disables it.

Safety net for anything seeded: C11 enters only at confidence 0.5 or above, at most 2 new positions a day, 4 open, 2 per thesis, 0.25 percent risk each, the don't chase and liquidity gates, plus the 4 week staleness expiry (v0.14.11) and the A6 bridge for theses that go nowhere.

## Tests

`tests/unit/test_v0_14_13.py`, 9 tests: rule 5 on, off, below threshold, overnight branch, rule 3 untouched, discard never copies, copy priority, config pins. Suite green.

## Services

- A5 is a oneshot (20:30 CT): items 1 and 2 are live from the next run, no restart.
- `a1-triage` restart for item 3 (the router runs inside A1). Evening window, watchdog paused.

## Verification

- `a1-triage` active, `config version active` for the v0.14.13 commit, no tracebacks; next morning `queue.messages` shows `signal.thesis` rows with `dedup_key` ending `:thesis_copy` and `body.symbols` populated.
- 2026-09-16 20:30 CT A5 digest: `deep: true`, `bootstrap: true`, `wide_items` about 80, and `new_theses` greater than 0 (`NEW_THESIS` decisions). If still zero after two deep nights in seeding mode, the model prompt is the next thing to look at, not the plumbing.

## Rollback

`git reset --hard v0.14.12` on `main`; restart `a1-triage`. A5 picks the old config up at its next run. Theses already created stay (they expire on their own if unsupported).

## Deploy record

Deployed 2026-09-15 21:14 CT (market closed): `c7-watchdog.timer` paused, `a1-triage` restarted, timer resumed. `a1-triage` active, `config version active 944a1e3cba1f`, zero errors, `triage` heartbeat fresh. A5 changes take effect at the 2026-09-16 20:30 CT run.
