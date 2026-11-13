# Current state, 2026-11-13 (Friday, written 09:10 CT)

Supersedes `claude_current-state-2026-11-10.md`. Written as a handoff for a fresh review session: take the conclusions in the two review documents as claims to test, not facts.

## Live

- Tree `v0.31.4`. Services: `c3-gate` v0.31.2 (10 Nov), `c1-ingestion` v0.31.3 (10 Nov), `a3-risk` and `c10-scanner` v0.31.0 (7 Nov), `c4-exec` and `a2-analyst` v0.30.0 (2 Nov). Timers read the tree at each run. All active, no restarts, one watchdog alert this week (regime snapshot timeout, recovered in 45 minutes).
- Account: equity 96,937. Week of 11-09: -47 on three scanner trades. November: -387. Inception (2026-07-14): -3.1 percent.
- Lanes: scanner live (window 09:33 to 15:15 ET, no time stop, 3.0 ATR trail, no scale out, 3 session cool off after a loss); news longs EDGAR only at half risk (live since 10 Nov 10:40 CT, before that dead for a week because of a gate bug, see v0.31.2); dilution shorts (offering class headlines) live at half risk; plain news shorts off; thesis lane paused (open positions none); trend day mode armed (not triggered yet); two shadow lanes journaling (earnings continuation 14 rows, +3.1 R with the move, 7 of 14; gap fade 1 row); A5 nightly claim 100 with 96 hour expiry since 10 Nov.
- A9: proposals 1 and 3 approved (evaluated 2026-11-14), proposal 2 rejected.
- Ingestion: 7 RSS feeds (GlobeNewswire's eight removed 10 Nov, 403 since 8 Oct; Benzinga carries the releases), EDGAR, Alpaca news stream (four reconnects this week). "Needs you": only `backup_offsite` (Google Drive token).

## Known defects and open items

1. Exit cancel race (`src/c4_exec/mechanics.py execute_exit`): the exit is submitted before the broker confirms the catastrophe stop cancel; "insufficient qty available" rejects, retried next bar; left INVX without a broker stop 2026-10-30 to 11-02. Fix designed in `docs/claude_month-review-2026-10.md` 1.3, not built. Also `c4-exec` exits on a DNS failure at startup (67 crash loop restarts after the 2026-10-05 host reset).
2. Breakeven ratchet (`scalp_v1 breakeven_at_R 0.75`) scratched two correct shorts on the 11-05 trend day; variants `be1.5` and `nobe` journaled nightly since 11-09; decide after 11-21. The first bar after a fill can count as progress (TSLA ratchet moved six seconds after entry).
3. Gate lane label: `lane_for` labels an up thesis `news_short` because it keys on horizon (always SHORT), not direction. Journal label only.
4. Normal day gross short budget: `shorting.max_gross_short_notional_pct 0.30` equals the scanner per position cap, so one scalp blocks the next (NVDA, TSM, INTC on 11-05). Trend day mode overrides it; normal days do not.
5. Analyst latency 24 to 30 s, about 400 model rejects a day for "has not moved"; the pre-check (v0.30.0) removes about 30 a day and defers the rest for five minutes. The GB10 llama.cpp b12050 build was 8 percent slower than the running b10970; Qwen4-27B not yet downloaded.
6. Watchdog has no rule for a repeating `message failed` line on a service with a fresh heartbeat (how the gate bug stayed hidden a week).
7. Overnight queue shows 400 to 500 waiting with the oldest near 72 hours by design (A4 forwards a budgeted handful, bulk expires at 72 h); dashboard label reads as a backlog.
8. Google Drive token for `backup-offsite`; GlobeNewswire decision is closed (removed).

## Reviews to challenge

`claude_october-loss-review-2026-11-02.md` (analyst direction has no edge; gates exclude the movers; earnings continuation and dilution shorts proposed) and `claude_thu-fri-review-2026-11-07.md` (trend day analysis; index rules weak over two years; single name shorts strong). Both rest on one to three months of journal. The scanner remains the only lane with a positive record (+1,648 since August before this month's -47).
