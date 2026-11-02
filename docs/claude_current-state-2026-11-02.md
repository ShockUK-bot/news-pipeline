# Current state, 2026-11-02 (written 15:10 CT, supersedes the 09:15 CT version of this file)

Supersedes `claude_current-state-2026-09-24.md`.

## Live

- Services run `v0.30.0` code (config version b42c8d03) since 15:05 CT: `c3-gate`, `a3-risk`, `c4-exec`, `c10-scanner`, `a2-analyst` restarted after the close with the watchdog timer paused; all active, heartbeats fresh, no errors, reconcile clean (equity 96,534). Tree is `v0.30.1` (A9 CLI fix only, no service touches it). Rollback for everything deployed today: `git reset --hard v0.26.4` and restart the same five services.
- Releases today (all pushed, patch notes in `docs/`): v0.27.0 (thesis entries paused, news shorts off, scalp exits without time stop or scale out and 3.0 ATR trail, scanner from 10:00 ET with a 3 session stop out cool off, news longs EDGAR only at half risk), v0.28.0 (earnings continuation and gap fade shadow lanes, journal only, A11 replay, A9 bar), v0.29.0 and v0.29.1 (dilution short lane live at half risk, exhaustion fade buckets named and watched by A9), v0.30.0 (deterministic analyst pre-check), v0.30.1 (A9 approve CLI registers its config version).
- A9 proposal 1 (scanner no scale out) APPROVED at b42c8d03; A9 evaluates it on 2026-11-07. Proposal 2 (3.0 ATR stop shadow) still PROPOSED, superseded in spirit by the trail and time stop changes; Ian to reject or leave.
- Positions: RIOT long 67 at 13.89 (thesis, catastrophe stop 8.49, lane paused for new entries) and PODD long 47 (news long from this morning). INVX closed 09:01 CT at 27.18 (-160.95) on operator instruction after its broker stop was found cancelled (cancel race, see the month review 1.3; code fix still pending as the v0.26.5 item, now folded into the next operational release).
- Reviews written today: `claude_month-review-2026-10.md` (outages), `claude_october-loss-review-2026-11-02.md` (why October lost, the market study, the plan). Upload both plus the patch notes to the design chat.

## Watching from tomorrow

- Shadow rows in `journal.scanner_candidates` status SHADOW from 09:45 ET; A11 replays them nightly into `journal.scanner_funnel_cf` (outcomes SHADOW_EARNINGS_CONTINUATION, SHADOW_GAP_FADE); A9 Saturday watch lines show progress to the 20 row bar.
- Analyst pre-check: ANALYST REJECT rows with `payload->>'precheck'='true'` and `queue.defer` activity; expect analyst call volume down about a sixth.
- Dilution shorts: GATE PASS rows with `payload->>'lane'='news_short_dilution'`; A3 sizes them at half risk.
- Scanner: first trades after 10:00 ET with the new exits; TICKER_COOLOFF rows for MRVL and PURR this week.

## Open items

1. llama.cpp bench b10970 vs b12050 at 16:17 CT today (build-2026-11 is ready); swap of the llama units is a separate evening change for Ian.
2. Cancel race fix and c4 startup retry (was v0.26.5): still to build.
3. GlobeNewswire 403 decision; Google Drive token for backup-offsite.
4. Qwen4-27B replay bench when weights land; triage model speed bench.
5. Thesis lane trend rule before it reopens; regime based lane switching; A6/A12 verdicts advisory.
