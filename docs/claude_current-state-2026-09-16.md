# Current state, 2026-09-16 (written the evening of 09-15, about 21:50 CT)

Supersedes `claude_current-state-2026-09-15.md`.

## Version and health

- Repo: tag `v0.15.0` on `main`, tree identical to the tag, pushed with tags.
- Running: every service on current code. New unit `c12-burst` active and enabled since 21:43 CT on 09-15 (real time burst stream, research only). Inference on llama.cpp b10970 on all slots since 09-15 evening.
- Shorting live (v0.14.8). Model labels correct (v0.14.9). A6 to C11 bridge live (v0.14.11). Open handoff floor and fade shadow live (v0.14.12). Thesis store seeding and router copy live (v0.14.13).

## What was done on 09-15 (evening session)

1. v0.14.11: A6 exit verdicts reach the exit ladder; A5 staleness 6 to 4 weeks. C11 armed RIOT (19.55) and INVX (29.77) at 21:15 CT; both should exit at the 09-16 open.
2. Gate Lab review and v0.14.12: open handoff long floor (`HANDOFF_UNMOVED`) and the fade shadow lane; `c3-gate` restarted 20:56 CT.
3. Thesis store investigation and v0.14.13: A5 deep nightly and seeding mode until 6 theses; router rule 5 copies tier 1 high urgency signals to the thesis lane; `a1-triage` restarted 21:14 CT.
4. "1 percent gain" design study: four backtests, no edge with current data; new data needed. `docs/claude_1pct-gain-design-2026-09-15.md`, scripts in `ops/research/`.
5. v0.15.0: C12 burst stream built, tested, deployed. `docs/claude_patch-notes-v0_15_0.md`.
6. Operator standing instruction recorded: an approved development item includes its deploy (timing rules still apply).

## Checks for the 09-16 session (in order)

1. 08:35 CT: RIOT and INVX exits (`journal.exits` with position ids 7 and 13; events carry `slip_px`). FRMI should be untouched.
2. From 08:36 CT: `journalctl -u c12-burst -f` shows `burst` lines; `stats` lines every 10 minutes show trades flowing (if `trades=0` during the session the stream is the problem). After 09:10 CT `ops/tools/burst_report.py` shows scored rows.
3. Morning: `HANDOFF_UNMOVED` vetoes on flat bullish handoffs; any `rule='fade'` rows in `journal.gate_counterfactuals`; `signal.thesis` rows with `:thesis_copy` dedup keys.
4. 06:00 CT A4 and 15:35 CT A7: first premarket and EOD runs on the new llama.cpp build (A6 nightly and A5 already ran fine on it).
5. 20:30 CT: A5 digest with `deep: true`, `bootstrap: true`, and `NEW_THESIS` decisions.

## Open items

1. C12 tuning after the first session (event rate, spread distribution); two week measurement before any lane decision.
2. Dashboard cosmetics: Gate Lab EH scoreboard not direction adjusted; fade rows shown under the RTH view; no C12 tab yet (`burst_report.py` covers it).
3. Model file tidy (July rollback GGUFs, 28 GB). Qwen3.8-Flash-Next evaluation for the heavy slot after the Qwen 4 window.
4. Scanner entry timing evidence (unchanged from 09-13).

## Files for the design chat

`claude_patch-notes-v0_14_11.md`, `-12`, `-13`, `claude_patch-notes-v0_15_0.md`, `claude_gatelab-review-2026-09-15.md`, `claude_1pct-gain-design-2026-09-15.md`, `claude_c12-build-handoff-2026-09-15.md`, and this file.
