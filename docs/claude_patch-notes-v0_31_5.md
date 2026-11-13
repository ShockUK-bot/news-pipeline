# Patch notes v0.31.5 (2026-11-13): scanner replay matches the live ladder; review challenge and trend day back test

Built during market hours on 2026-11-13. No service was restarted and no config was changed. The code is read only by the A11 nightly run (15:20 CT timer) and by command line tools.

## 1. What changed and why

Every nightly replay variant in `journal.scanner_counterfactuals` was one change away from the ladder that was live before v0.27.0 (1.5 ATR trail, 60 minute time stop, 50 percent scale out). Since 2026-11-02 the live ladder is different (3.0 ATR trail, no time stop, no scale out), so:

- no variant could be compared with the live result (November: `base` said +151, live was -380);
- `be1.5` and `nobe` (v0.30.2) asked the breakeven question of a ladder nobody trades, and only for trades closed after they shipped (3 trades).

Changes:

- `ops/tools/scalp_replay.py`: `replay()` takes `trail_at`, `time_win`, `time_min_r` and `tf` as parameters. New fixed variants `v27` (the v0.27.0 ladder), `v27_be1.5`, `v27_nobe`. New `policy_kwargs()` and the `asrun` variant: each trade replayed under its own journaled `exit_policy`. Promoted scalps (profit lock, session time stop) get no `asrun` row because the replay does not model them.
- `ops/scanner_counterfactuals.py`: a position is pending when it is missing ANY current variant (was: when it had no rows at all), so new variants are backfilled for all history. Inserts stay `ON CONFLICT DO NOTHING`; nothing is updated or deleted.
- `tests/unit/test_v0_31_5.py`: six tests. Full suite: 955 passed, 1 skipped.

The first A11 run after this release (2026-11-13 15:20 CT) backfills the new variants for all 51 closed scanner trades.

## 2. Dry run over all 51 scanner trades (nothing written)

| Period | n | Live result | `base` (old ladder) | `v27` (live ladder) | `v27_nobe` |
|---|---|---|---|---|---|
| Aug to Sep | 24 | +1,487 | +1,770 | +2,689 | +2,607 |
| October | 20 | +160 | -244 | +1,622 | +2,184 |
| November | 7 | -380 | +151 | -548 | -893 |
| All | 51 | +1,268 | +1,678 | +3,763 | +3,898 |

- Fidelity: `asrun` is within 50 dollars of the live result on 28 of 45 trades, average gap 58. November `asrun` -548 against live -380 (live breakeven exits filled a little better than the stop price).
- Breakeven ratchet: removing it changes 7 of 51 trades, helps 2 (NBIS 10-14 +562, TSLA 11-05 +360) and hurts 5 (AVGO 11-05 -485, BE -132, RKLB -88, two of about -42). Net +135. Not a case for changing it.
- `v27_be1.5` equals `v27_nobe` on every trade: the trail takes over at 1 R, so a breakeven at 1.5 R never acts.
- The live ladder depends on rare runners. Four trades (MSTR 09-18, ASTS 10-09, CBRS 10-12, NBIS 10-16) supply +5,923 of `v27`; the other 47 sum to -2,160 under `v27` and about +59 under `base`. Replay fills are at the stop price with no slippage.

## 3. Trend day mode back test (read only; scripts in the session scratchpad, not in the repo)

Exact live rule from `config/trend_day.yaml`: from 10:00 ET, QQQ or SPY at or below -2 percent against the previous close and below its own 09:45 price; the first hit holds for the session. Two years of 5 minute bars, 510 sessions. The test picks today's trigger at 10:15 ET, the same minute the live system set the flag (09:15 CT).

- Frequency: 47 sessions before today, about one in eleven.
- Index after the trigger, to 15:50 ET: kept falling on 25 of 47 days, mean -0.15 percent, median -0.04. Triggers by 10:30 ET: 12 of 20, mean -0.35.
- Single names (314 liquid names taken from the scanner's own candidates, 100 million dollars a day or more), short from the trigger to 15:50:

| Group | Days | Mean of daily means | Days positive | Worst day | Best day |
|---|---|---|---|---|---|
| All names | 47 | -0.05 pct | 25 | -2.63 | +2.07 |
| Down 4 pct or more at the trigger | 47 | -0.12 pct | 27 | -4.86 | +4.12 |
| Same, trigger by 10:30 ET | 20 | -0.07 pct | 10 | -3.85 | +4.12 |
| Same, with a 2.5 pct stop | 47 | -0.17 pct | 21 | | |

Conclusion: the trigger does not select trend days. 2026-11-05 (+2.16 percent on the down movers) was one of the good days; 2025-11-07, 2026-07-28, 2025-02-03 and 2024-12-30 each lost 3.5 to 4.9 percent on the same trade. The mode doubles short heat on a coin flip with fat tails both ways. Limits: the universe is today's liquid names (survivorship), 5 minute bars, no costs.

Live today (2026-11-13): the flag was set at 09:15 CT and the scanner opened AMD, SNDK and IREN shorts within two minutes.

## 4. Review challenge, summary (full detail in the session report)

Tested against the live journal on 2026-11-13:

- Holds: thesis lane pause (12 trades, 3 winners, -1,562); the analyst has no direction skill (up calls wrong in all four months); fade stays off (down mover fade +2.3 R, +14.5 R, then -24.1 R in November).
- Does not hold: dilution shorts (the tradeable subset, 112 cases at 3 dollars and up and borrowable, fell 0.14 percent on average and was positive in one month of four; the edge was in unborrowable names under 3 dollars); the 09:33 scanner start (+6.47 R of the +6.26 R came from the ten candidates on 11-05; nothing is emitted before 09:45 ET anyway); EDGAR longs (6 trades, 2 winners, -437; EDGAR up calls right 45 percent of 352); the earnings shadow's +3.1 R is two trades and the earnings calendar keeps superseded dates (NBIS flagged three days running); trend day mode (section 3).
- Scanner entries before 10:00 ET: 33 trades, -257. After 10:00: 18 trades, +1,525. Suggestive, about one standard error.

## 5. Proposed for the evening deploy (NOT applied; each needs the operator's yes)

1. `config/shorting.yaml` `news_short_dilution: false`.
2. `config/scanner.yaml` `session_start_et: "10:00"`.
3. `config/trend_day.yaml` `enabled: false`.
4. EDGAR longs to shadow.

Services for 1 to 4: `a3-risk`, `c3-gate`, `c4-exec`, `c10-scanner`, after 15:00 CT with `c7-watchdog.timer` paused.

## 6. Services restarted

None.

## 7. Rollback

`git revert` the v0.31.5 commit (or `git checkout v0.31.4 -- ops/tools/scalp_replay.py ops/scanner_counterfactuals.py`). No restart needed. Rows already written under the new variant names are additive and can stay.
