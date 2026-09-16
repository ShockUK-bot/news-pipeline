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

## Morning checklist results (09-16, 08:35 to 08:55 CT)

1. **INVX exited at the open**: STOP 48 at 29.71 at 08:35, −36.96 realised, `slip_px` +0.06. **RIOT did not**: it gapped up to 20.11, above its 19.55 stop, so the tighten only exit is still armed and fires on the first dip below 19.55. Design note for a later release: a review exit could sell at the next open instead of arming a stop, so a gap up cannot keep a broken thesis alive. FRMI untouched.
2. **C12**: no restarts, no stream errors, 2.5 million trades by 08:45, six detections in the first ten minutes (DIS, HPQ, ANET, LITE long; UNH, WFC short), spreads 4 to 19 bps. BURST tab added to the dashboard (v0.15.1).
3. **Thesis lane copies**: 13 `thesis_copy` rows by 08:45, all with tickers (`body.triage.tickers`). A5's first seeding run is tonight 20:30 CT.
4. **Heavy slot on b10970 was 26 percent slower** (A4 p50 248 s vs 141 s; 7.8 vs 10.6 tok/s at the same prompt). Rolled the heavy unit back to `build/` (b10064) as v0.15.2; installed without a restart, first run A7 at 15:35 CT. Triage (2.8 s) and analyst (18 to 19 s) unchanged on b10970, no regression there.
5. **Open handoff floor works**: by 09:08 two bullish handoffs were vetoed `HANDOFF_UNMOVED` (VTRS, PAA). No fade candidates yet (today's extended movers were bearish micro caps: XBIO, ARTL, XCUR, TOPS). The gate was busy as normal: 14 regular hours verdicts by 09:10 against 10 and 9 on the two prior sessions; 50 analyst rows and 15 theses since the open; scanner passes CRCL short and SPCX long.
6. **C12 first scores** (09:08): 16 detections by 09:02. DIS (long) hit the 0.7 percent stop 38 seconds after detection (best +0.30, worst −0.76, 30 min +0.05); HPQ (long) hit the stop at 37 seconds (worst −2.63, 30 min −1.15). The remaining fourteen score through the morning; the BURST tab shows them live. Consistent with the backtest: bursts in liquid names reverse first more often than they continue. Two weeks of this decides the lane.
7. Overnight lane: 39 messages enqueued 06:00 to 08:30 still waiting after the open. Checked against `late.py`: by design, the late passes (every 10 minutes 06:00 to 08:59 CT) forward a paced allowance to the analyst and defer the rest; whatever is still deferred at the open waits for tomorrow's 06:00 sheet or expires. Not a fault.
8. A query note for future sessions: `current_date + time 'HH:MM'` is read in the session time zone (Chicago), so compare with `(ts at time zone 'America/Chicago')::time`, not with UTC clock times. An earlier "no verdicts" reading this morning was that mistake.

## End of day: BURST scoreboard, session 1 (checked 15:50 CT)

158 detections across the day (15 in the 08:00 hour, 32 in the 10:00 hour, 73 in the 13:00 hour: **FOMC decision day**, the 13:00 CT print and the 13:30 press conference dominate the sample). Spreads averaged 9 bps. Zero `news_anchored` rows (no A1 escalation coincided with a burst). Scoring bracket: +1 percent target versus −0.7 percent stop within 30 minutes, entry at the detection price, cost 10 bps.

| Window | Rule | n | Target first | Stop first | Avg 30 min | After cost |
|---|---|---|---|---|---|---|
| Before 13:00 | fade an UP burst (short) | 54 | 39% | 22% | +0.52% | **+0.42%** |
| Before 13:00 | chase an UP burst (long) | 54 | 9% | 70% | −0.52% | −0.62% |
| Before 13:00 | fade a DOWN burst (long) | 19 | 37% | 16% | +0.28% | +0.18% |
| Before 13:00 | chase a DOWN burst (short) | 19 | 11% | 63% | −0.28% | −0.38% |
| FOMC 13:00 to 14:30 | fade an UP burst (short) | 20 | 70% | 15% | +1.27% | +1.17% |
| FOMC 13:00 to 14:30 | chase a DOWN burst (short) | 60 | 25% | 57% | +0.25% | +0.15% |
| FOMC 13:00 to 14:30 | fade a DOWN burst (long) | 60 | 40% | 38% | −0.25% | −0.35% |

Read: on session 1 the liquid universe mean reverts after a burst in both directions, strongest for up bursts, in and out of the Fed window. Momentum chasing, the original "1 percent gain" idea, loses on every cut, which matches the backtest. The fade result is one session, half of it an FOMC afternoon, so it goes into the two week measurement rather than a lane; the go live bar (200 scored, +0.15 percent after cost, no more than 40 percent of sessions negative) stands. Caveats to keep in mind when reading later sessions: the fade entry is assumed at the burst's last trade (a real short would sell into the burst at the bid and needs borrow), and the fade rows are exact mirrors of the momentum rows by construction.

A7 EOD at 15:35 ran on the restored heavy build (b10064): success, 21.8 tok/s on a 297 token output, no errors.

## Open items

1. C12 tuning after the first session (event rate, spread distribution); two week measurement before any lane decision.
2. Dashboard cosmetics: Gate Lab EH scoreboard not direction adjusted; fade rows shown under the RTH view; no C12 tab yet (`burst_report.py` covers it).
3. Model file tidy (July rollback GGUFs, 28 GB). Qwen3.8-Flash-Next evaluation for the heavy slot after the Qwen 4 window.
4. Scanner entry timing evidence (unchanged from 09-13).

## Files for the design chat

`claude_patch-notes-v0_14_11.md`, `-12`, `-13`, `claude_patch-notes-v0_15_0.md`, `claude_gatelab-review-2026-09-15.md`, `claude_1pct-gain-design-2026-09-15.md`, `claude_c12-build-handoff-2026-09-15.md`, and this file.
