# Patch notes v0.27.0 (2026-11-02): stop the bleeding

Operator approved all five items of the "week 1" list in `docs/claude_october-loss-review-2026-11-02.md` on 2026-11-02. Config and small code changes only; no new lane. Deployed after the close on 2026-11-02 (see Services).

## Changes

1. **Thesis lane paused.** `config/thesis_entry.yaml` `max_new_per_day: 0`. October: five longs, none ever in profit, -1,140; the lane bought RIOT four times on the way down. Open RIOT (67 at 13.89) keeps its stops and the nightly A6 review. Reopens only with a price and sector trend rule.
2. **News shorts off.** `config/shorting.yaml` `lanes.news_short: false`. 21 trades, -1,209 since 2026-08-26; the analyst's direction call scores as a coin flip over 5,001 gate counterfactuals. Down theses now journal GATE VETO `LONG_ONLY` and keep their counterfactual rows, so the lane can earn its way back. Scanner shorts and the fade shadow are untouched.
3. **Scalp exits.** `config/exit_profiles.yaml` `scalp_v1`: `time_stop: null` (was 60 minutes at under 0.5 R; the seven October TIME exits then ran +2.99 R on average and the journaled "notime" variant turns the lane's month from -244 to +707), `trail.k: 3.0` (was 1.5; the journaled trail3.0 variant is +371 against base), `realization.action: none` (A9 proposal 1 of 2026-10-17, approved; the 60 percent target is still journaled by A11). `src/c4_exec/exits.py` learns the `none` action.
4. **Scanner entries.** `config/scanner.yaml` `session_start_et: "10:00"` (was 09:50; the five first minute stop outs cost 2,097) and `stop_out_cooloff_sessions: 3`: no scanner re-entry in a name for three sessions after a losing scanner trade in it (CRWV three days running, -1,327). `src/c10_scanner/service.py` journals the skip as `FILTERED / TICKER_COOLOFF` once per day per name; `src/c10_scanner/rules.py` `cooloff_calendar_days`. The 09:33 early shadow window is unchanged. One attempt per ticker per day was already the behaviour (emitted names are skipped for the rest of the day).
5. **News longs: EDGAR only, half size.** `config/gate.yaml` `news_long_sources: [edgar]`; `src/c3_gate/rules.py` `lane_source_filter` turns a PASS for an up thesis from any other source into a final `VETO LANE_SOURCE` after evaluation, so the counterfactual row is still recorded. `config/risk.yaml` `news.long_risk_multiplier: 0.5`; `src/a3_risk/sizing.py` `news_capital_cfg` halves the risk budget for news longs only (`src/a3_risk/service.py`). EDGAR 8-K longs were 3 for 3 in October; Benzinga longs are -684 since inception.

Version `pyproject.toml` 0.27.0. Tests: `tests/unit/test_v0_27_0.py`; two older config pins updated (`test_v0_24_0.py` session start, `test_v0_26_2.py` trail k follows the live profile). Suite 922 passed.

## Expected effect

On October's trades: thesis +1,140, news shorts +988, scanner exits about +950, scanner first minute entries about +1,700 (not additive, overlapping trades). A scanner plus EDGAR longs system would have been roughly +700 to +1,300 for the month instead of -1,693. That is the baseline for v0.28.0 and v0.29.0, not a target.

## Services

Restart after the close: `c3-gate`, `a3-risk`, `c4-exec`, `c10-scanner`. `thesis-entry.timer` (21:15 CT, oneshot) reads the config on its next run; no restart. `c7-watchdog.timer` stopped during the restarts and started after. After the restarts: `python -m a9_review.service --approve 1 --config-version <v0.27.0 commit>` marks proposal 1 APPROVED so A9 evaluates it on 2026-11-07.

Rollback: `git reset --hard v0.26.4` then restart the same four services. Previous tag v0.26.4.
