# Current state, 2026-09-15 (end of session, about 15:55 CT)

Supersedes `claude_current-state-2026-09-13c.md`.

## Version and health

- Repo: tag `v0.14.10` on `main`, tree identical to the tag, `main` and `origin/main` in sync.
- Running: all agents and components on v0.14.9 code (identical to v0.14.10 for every runtime file). Inference on llama.cpp b10970 (`build-2026-09/`) for triage, analyst and heavy since this evening.
- Shorting mode `live` since the 09-14 evening restart (v0.14.8). Journal model labels correct since the same restart (v0.14.9).
- All units active, heartbeats fresh, no errors.

## Done since the last file (09-14 and 09-15)

1. **Short selling was off for three weeks without anyone knowing.** `shorting.mode` went back to `shadow` in a `git reset` on 08-22 (the running A3 kept `live` in memory until the 08-27 reboot). Flipped to `live` and committed as v0.14.8; gotcha added to `CLAUDE.md`. First live short session was today, 09-15; check `journal.positions` for `side = SHORT` rows.
2. **Model review** (see the 09-14 conversation summary in the patch notes): the analyst slot has served Qwen3.8-27B since 08-22 while every config said 3.6, the same lost commit. Corrected as v0.14.9. Upstream: Qwen3.8-27B is still the newest dense mid model; no Qwen3.8 small model; Qwen3.8-Flash-Next (125B MoE, 6B active, Qwen4 architecture preview) is the only candidate for the heavy slot; Qwen 4 rumoured 22 to 24 September.
3. **llama.cpp upgraded to b10970** on all three slots (v0.14.10): analyst 17 percent faster at p50 on a fair bench, triage and heavy smoke tested. `/opt/llama.cpp` now owned by `trader`. `--no-mmap` is gone upstream; `--load-mode none` replaces it.
4. Bench script repaired (broken SQL join, and endpoint A never received the thinking off kwarg, so all earlier A/B numbers were biased in the candidate's favour).
5. Deploy note: the sudo rule for `cp` into `/etc/systemd/system/` only matches the absolute `/opt/pipeline/ops/systemd/...` path.

## Added later on 09-15: v0.14.11

Operator asked why nightly and morning emails kept recommending exits (RIOT) that never happened. Cause: A6 was recommendation only by design, no operator exit path existed, and the thesis lane's only time stop is the thesis store, whose 6 week staleness clock disagreed with A6's 4 week one. Operator chose no human in the loop. Built: the A6 to C11 bridge (three consecutive "exit, thesis broken" nightly verdicts arm the tighten only exit) and A5 `stale_weeks` 6 to 4. Oneshots, no restart. Expected first effect: RIOT and INVX exit at the 09-16 open. See `claude_patch-notes-v0_14_11.md`.

## Added later on 09-15: Gate Lab review and v0.14.12

`docs/claude_gatelab-review-2026-09-15.md`: 2,567 shadow outcomes say bullish news longs at the open have no edge (the live open handoff lane is −1,254 over 18 trades), bearish shorts do, and bullish 4 to 12 percent gaps fade into the close. Operator chose: fade lane as a shadow, open handoff floor now. v0.14.12 deployed 20:56 CT (`c3-gate` restart): `HANDOFF_UNMOVED` floor at 2 percent for bullish open handoffs (would have blocked 32 of the last 33), fade shadow lane journaling `rule='fade'` rows (expect 2 to 6 a week; first read at about 20 rows). Heavy slot confirmed fine on b10970 (A6 nightly and A5 both ran, zero errors).

## Open items

1. **First heavy runs on b10970**: A6 nightly 19:00 CT today, A4 premarket 06:00 CT tomorrow, A7 eod 15:35 CT tomorrow. Check each journal once; A4 p50 was 141 s per call and should improve.
2. RESOLVED: **first live short happened today.** CRCL, scanner lane, RISK `SIZE` with `side: SHORT`, 152 shares short at 88.69 at 08:52 CT, closed the same session for +30.71. The short path (sizing, order, exit ladder) works end to end on v0.14.9 code. Keep an eye on the next few for exit behaviour and slippage (`slip_px` is journaled now).
3. **Qwen3.8-Flash-Next evaluation** for the heavy slot: needs a GGUF (Unsloth UD-Q4_K_XL is the one reported working on a DGX Spark), the `qwen4_exp` support in b10970 (present upstream; confirm on this build), a memory profile during a real A4 run, and an off hours bench on :8082. Also decide whether to wait for the Qwen 4 announcement window first.
4. **Model file tidy**: `Qwen_Qwen3-32B-Q5_K_M.gguf` (22 GB) and `Qwen_Qwen3-8B-Q6_K.gguf` (6 GB) are July rollback targets no unit references. Delete when convenient.
5. Carried from 09-13: scanner entry timing evidence (30 or more scanner longs, rejected pool forward returns, shadow short outcomes, stop fill quality now that `slip_px` is journaled); test warnings in `test_cik_map.py`.

## Next steps

- Tomorrow after the close: read the three heavy journals and the first short trade; write a short note.
- Then decide on the Flash-Next bench (item 3).

## Files for the design chat

`claude_patch-notes-v0_14_8.md`, `claude_patch-notes-v0_14_9.md`, `claude_patch-notes-v0_14_10.md`, `bench-analyst-b10573-vs-b10970-2026-09-14.txt`, and this file.
