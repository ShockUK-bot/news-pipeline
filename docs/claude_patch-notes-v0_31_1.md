# Patch notes v0.31.1 (2026-11-07): A9 proposal 3 applied, proposal 2 rejected

Operator decision 2026-11-07: approve proposal 3, reject proposal 2.

- **Proposal 3 (A9, 2026-11-07): scanner window from 09:33 ET.** `config/scanner.yaml` `session_start_et: "09:33"` (was 10:00 since v0.27.0, 09:50 before that). Evidence: the 09:33 to 10:00 shadow window replayed +5.6 R over 37 candidates with 24 winners in the four week window, and on the 2026-11-05 trend day the ten early window shorts were the best bucket (+6.5 R). The early window shadow (`CAPPED / EARLY_WINDOW`) therefore becomes live emission; the journaling of everything seen is unchanged. Guards that stay: the stop out cool off, no time stop, the 3.0 ATR trail, trend day caps. Watch: October's first minute stop outs were 09:50 to 09:52 entries with 2 ATR(5m) stops; the same can happen at 09:35. A9's success metric for this proposal is weekly `sum_r > 0` for two weeks; if the early entries stop out, the rollback is one line (`"10:00"`).
- **Proposal 2 (A9, 2026-10-31): shadow a 3.0 ATR scalp stop.** REJECTED. The journaled stop3.0 variant was worse than base on October's trades (-529 against base) and the trail and time stop changes of v0.27.0 addressed the stop inefficiency it targeted.

Files: `config/scanner.yaml`, two config pins (`test_v0_24_0.py`, `test_v0_27_0.py`). Version 0.31.1. Suite 948 passed. Service: `c10-scanner` restart (Saturday, market closed). Rollback: `session_start_et: "10:00"` and restart `c10-scanner`.
