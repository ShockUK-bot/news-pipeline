# Current state, 2026-11-10 (Tuesday, written 10:55 CT)

Supersedes `claude_current-state-2026-11-07.md`.

## Live

- Tree `v0.31.3`. Running: `c3-gate` on v0.31.2 (restarted 10:40 CT on the operator's "do it now"), `c1-ingestion` on v0.31.3 (restarted 10:49 CT, same instruction), `a3-risk` and `c10-scanner` on v0.31.0 (Saturday), `c4-exec` and `a2-analyst` on v0.30.0 (11-02). All active, no restarts, heartbeats fresh.
- **Gate bug fixed (v0.31.2):** the v0.27.0 and v0.29.0 gate edits had landed in the extended hours shadow branch, so every in session news thesis from 2026-11-02 15:05 CT to 2026-11-10 10:40 CT raised NameError at the gate and was dropped (272 messages). The EDGAR long lane and the dilution short lane therefore only start now. The scanner lane, pre-check, shadow lanes and trend day scanner side were unaffected. First in session news thesis through the repaired branch: being watched (none had moved enough to leave the pre-check by 10:55 CT).
- **RSS audit (v0.31.3):** eight GlobeNewswire feeds removed (403 to every client since 10-08; Benzinga via Alpaca carries the releases, 1,914 a week); `nasdaq-halts` on https; 7 feeds polling and OK. The eight DEGRADED health rows deleted with the operator's approval (8 rows). "Needs you" in the digest should now show only `backup_offsite`.
- Monday 11-09 verdicts: trend day mode correctly silent (QQQ +3.4 percent day); shadow lanes journaled IONS and CECO (both replays -1 R), six more today; scanner window from 09:33 ET emitted five names, MRVL long +290 to the force flat. Today: NVDA short 09:53 ET stopped in 8 minutes, -366 (early entry pattern), and the exit cancel race fired once more (fix still pending).
- A9: proposals 1 and 3 approved and under evaluation (11-14), proposal 2 rejected.

## Open items

1. Confirm the first news thesis passes the gate (watcher running); then the dilution lane and EDGAR longs are finally live.
2. Cancel race fix and c4 startup retry (October item, hit again today on NVDA).
3. Breakeven ratchet variants being journaled; decide after 11-21.
4. Normal day gross short budget (0.30 per position = gross cap); config proposal pending.
5. Watchdog rule for repeating `message failed` lines on a healthy service (lesson from v0.31.2).
6. Google Drive token; Qwen4 bench when weights land.
