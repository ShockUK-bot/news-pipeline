# Patch notes v0.31.6 (2026-11-13): four switches after the review challenge

Built during market hours on 2026-11-13 in a separate copy of the repo, so no running service could pick up a half finished change. Deployed after the close. Evidence for all four is in `docs/claude_patch-notes-v0_31_5.md` (sections 3 and 4).

## 1. What changed and why

1. **Dilution shorts off.** `config/shorting.yaml` `lanes.news_short_dilution: false`. The lane made zero trades. The tradeable subset (3 dollars and up, borrowable, 112 cases) fell 0.14 percent on average and was positive in one month of four; the edge quoted when it was built was in unborrowable names under 3 dollars. Vetoes keep their counterfactual rows, so the lane is still measured.
2. **Scanner start back to 10:00 ET.** `config/scanner.yaml` `session_start_et: "10:00"`. The early window from 09:33 stays on in shadow (candidates journaled `CAPPED_EARLY_WINDOW`, nothing emitted), which is the v0.27.0 arrangement. The +5.6 R that carried A9 proposal 3 was one day (+6.47 R from ten candidates on 11-05). Live entries before 10:00 ET: 33 trades, -257. After: 18 trades, +1,525.
3. **Trend day mode off.** `config/trend_day.yaml` `enabled: false`. Back test of the exact rule over 510 sessions: fired on 47, the index kept falling on 25, shorting the down 4 percent movers from the trigger to 15:50 ET averaged -0.12 percent a day (27 of 47 days positive, worst -4.86, best +4.12). The code stays; only the switch changes. With it off the caps return to 2 concurrent scanner positions, 2 per scan, 6 per hour, gross short 0.30, and news shorts stay off on down days.
4. **News longs to shadow.** New key `config/gate.yaml` `news_long_shadow: true` and a small change in `src/c3_gate/rules.py` `lane_source_filter`. A news long that passes the gate and the EDGAR source rule now journals `GATE VETO LANE_SHADOW` with a counterfactual row and places no order. Other sources keep `LANE_SOURCE`, so the two samples stay separate. EDGAR longs since v0.27.0: 6 trades, 2 winners, -437; EDGAR up calls right 45 percent of 352. Set the key to false to trade EDGAR longs again.

After this release the only lane that places orders is the scanner (long and short, from 10:00 ET). Everything else is measured in shadow.

## 2. Files

- `config/shorting.yaml`, `config/scanner.yaml`, `config/trend_day.yaml`, `config/gate.yaml`
- `src/c3_gate/rules.py`
- `tests/unit/test_v0_31_6.py` (new, five tests); `tests/unit/test_v0_24_0.py`, `test_v0_27_0.py`, `test_v0_29_0.py`, `test_v0_31_0.py` (pinned config values updated; the trend day rules are still tested with the switch forced on)
- `pyproject.toml` 0.31.5 to 0.31.6

No migration. `journal.decisions.veto_reason` and `journal.gate_counterfactuals.veto_reason` are free text with no check constraint, verified on the live database.

## 3. Tests

Full suite in the build copy: 960 passed, 1 skipped.

## 4. Deploy and verification

DEPLOY_SECTION

## 5. Rollback

`git revert` the v0.31.6 commit on `main` (or restore the four config lines and `src/c3_gate/rules.py` from `v0.31.5`), then:

`sudo -n systemctl stop c7-watchdog.timer && sudo -n systemctl restart a3-risk c3-gate c4-exec c10-scanner && sudo -n systemctl start c7-watchdog.timer`

Evening only. Each switch can also be reverted on its own by restoring its one line and restarting the services that read it (`shorting.yaml`: a3-risk, c3-gate, c4-exec; `scanner.yaml`: c10-scanner; `trend_day.yaml`: a3-risk, c3-gate, c10-scanner; `gate.yaml`: c3-gate).
