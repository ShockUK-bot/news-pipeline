# October 2026 month review (written 2026-11-02, 08:40 to 09:15 CT)

Scope follows `docs/claude_restart-brief-2026-09-22.md`. The system ran unattended from 2026-09-24 to 2026-11-02. Everything below was read from the live journal, systemd and the service logs on 2026-11-02; nothing was changed.

State at the time of the review: `v0.26.4` live on `main`, `git diff v0.26.4 --stat` empty, remote in sync, 916 unit tests pass. All 12 long running services active, all 22 timers ran successfully at their last trigger, broker equity 96,996 at 08:56 CT.

## 1. Incidents and outages

### 1.1 Host reset during market hours, 2026-10-05 11:58 CDT (resolved by itself)

The box went down hard at 11:58:52 CDT and was booting again at 11:59:35. The journal file was "corrupted or uncleanly shut down", there is no kernel panic, no out of memory kill and no shutdown request in the log, so this was a power cut or a hardware reset; the cause is not recorded. It was the first reboot since 2026-08-27.

- Every service came back at 11:59:41 except `c4-exec`, which crashed 67 times in a row (one attempt every 5 seconds) because DNS was not resolving yet: it makes a broker call at startup and exits when that fails. It stayed up from 12:05:33. The dead-man then blocked entries from 12:05:34 to 12:06:37 while heartbeats caught up.
- Trading was effectively dark for about 8 minutes (11:58 to 12:06). No position was hurt: PBR's trail exit had filled at 11:49, and the two open news positions (RIVN, ECHO) re-armed their invalidations at 12:05.
- No watchdog alert and no email mentioned the reboot (a single stale pass does not alert, by design). Ian only finds out by looking.
- Follow ups: `c4-exec` should retry the startup broker call with backoff instead of exiting (`ops/systemd/c4-exec.service` also lacks `Wants=network-online.target`, unlike `c1-ingestion`); the evening digest should state "host rebooted at HH:MM" when the boot id changed during the day.

### 1.2 GlobeNewswire blocked since 2026-10-08 (still open, needs a decision)

Eight of the 15 RSS feeds (`globenewswire-public`, `gnw-classaction`, `gnw-manda`, `gnw-clinical`, `gnw-earnings`, `gnw-insider`, `gnw-management`, `gnw-ipo`) have answered `403 Forbidden` on every poll since 2026-10-08 (ramping on the 8th and 9th, zero items from the 10th). Before that they delivered about 600 items a day, roughly 12 percent of all ingested items and about a third of the press release wire. A plain `curl` from the box gets 403 too, so the pipeline's honest `news-pipeline/0.13.4` identifier from v0.13.4 is now refused as well; Akamai is blocking this client or this address.

- Side effect: 190,000 `poll failed` error lines in the `c1-ingestion` log in October (one per feed per 15 seconds), and the same nine "needs you" lines in every evening digest since 2026-10-08.
- Options for Ian: (a) re-probe `User-Agent` variants from the Spark and update `config/sources.yaml` (the 2026-08-15 probe table in that file is the model); (b) while blocked, back the eight feeds off to an hourly poll so the log and the digest stop shouting; (c) drop them. (a) needs Ian's say so because the earlier probes showed GlobeNewswire tarpits browser impersonation.

### 1.3 Exit cancel race left INVX without its broker stop (URGENT, still open)

Every synthetic exit first cancels the broker resident catastrophe stop, then submits the exit. Alpaca acknowledges the cancel asynchronously; at the open and in the last five minutes it can take 30 to 60 seconds. When the exit goes in before the cancel completes, the broker answers `insufficient qty available for order (requested: N, available: 0)` because the stop still holds the shares. The code then tries to re-place the catastrophe stop at once, which fails for the same reason, and the error escapes as `engine step failed for position` with no journal event.

Occurrences in October: 2026-10-06 OPCH 14:55, 2026-10-07 OPCH 08:30, 2026-10-09 INVX (position 49) 08:35, 2026-10-12 CBRS 09:05, 2026-10-15 RIOT 08:30, 08:32 and 08:35, 2026-10-29 V 14:55, 2026-10-30 V 08:32 and 14:45, 2026-10-30 INVX (position 87) 08:32. In every case but the last the next bar retried and the exit filled, at a worse price (RIOT position 77 filled at 14.05 against a 14.73 stop after three tries).

The last case is the problem. On 2026-10-30 the 08:30 CT bar for INVX printed 25.98 against the 26.21 synthetic stop, the STOP layer fired, the exit was rejected, and the broker completed the cancel of catastrophe stop order 440 (stop 24.79) at 08:32:55 CT. Nothing re-placed it. The journal still shows order 440 as `ACCEPTED`; Alpaca shows it `canceled`. INVX recovered to 28 that day, so the position is still open (87 shares, entry 29.03), and as of 2026-11-02 08:56 CT the broker holds exactly one open order, the RIOT catastrophe stop. INVX has had no tier 1 stop for two sessions and today trades between 26.34 and 26.60, within 1.5 percent of the synthetic stop.

- The synthetic STOP still works: when it fires, the cancel of order 440 returns "not cancellable" (v0.26.4 behaviour), the fill check sees `canceled`, and the exit proceeds. What is missing is the protection when the engine is not evaluating: INVX also tripped the halt heuristic five times in October ("no bar for 10 minutes" on a thin stock, 2026-10-15, 10-20, 10-23, 10-26, 10-27), and during a freeze no stop layer runs.
- Immediate action for Ian to approve: re-place a sell stop for 87 INVX at 24.79 at the broker and point `journal.positions.catastrophe_stop_order_id` at the new order row (one INSERT into `journal.orders`, one UPDATE of position 87, both to be shown to Ian before running). Alternative: tell Claude Code "close INVX now".
- Dev fix (proposed v0.26.5): after `broker.cancel`, poll `get_order` until the status is `canceled` (or 20 seconds) before submitting the exit; retry `_place_catastrophe` with backoff and journal `GUARD_ACTION` when it is rejected; teach `reconcile` to detect an open position whose catastrophe order is `canceled` or absent at the broker and re-place it; update the journal order row on cancel so the dashboard stops showing 62 "ACCEPTED" stops for closed positions.

### 1.4 Smaller items

- Alpaca news websocket dropped and reconnected four times (2026-10-07, 10-14 twice, 10-20). The `alpaca_benzinga` five minute gaps during the day are quiet periods, not outages.
- 861 analyst `data-unavailable REJECT` rows (about 10 percent of analyst calls): Alpaca returns 404 snapshots for tickers it does not carry (OTC, foreign). Correct behaviour, but a cheap pre-filter in triage would save the queue hops.
- One triage message (msg 402581, 2026-10-30 07:36) died after five `400 Bad Request` answers from `llama-a1`, most likely a prompt over the context limit. One item lost.
- The kernel logged 57 `NVRM ... Out of memory` lines in this boot, all at 06:00, 15:36 and 19:01 CT, which is when `a4-premarket`, `a7-eod` and `a6-nightly` batch their analyst calls. All three jobs succeeded every day. Watch item; it was also present before the 2026-10-05 reset but not at the time of the reset.
- Analyst latency crept from 19.8 s average (week of 2026-10-05) to 25.6 s (week of 10-26) to 30.4 s this morning, with triage volume up 50 percent in the last week of October (earnings season). The queues are empty, so it is load, not a backlog.
- `backup_offsite` has been DEGRADED all month: the Google Drive token was never created. Local dumps (14 kept, 3.4 GB) and the GitHub `journal-extract` push ran every night.
- An analyst message from 2026-09-03 (msg 186006) is still "claimed" and will never finish; harmless, but `queue-prune` only prunes done rows.
- Watchdog: 24,507 passes since the reboot, zero alerts in October. The only alert since the last review was the 2026-09-24 `scanner_cf` warning. The watchdog checks freshness only, so nine DEGRADED health rows never produced an alert; only the digest showed them.

## 2. Performance

### 2.1 Account

| Date | Broker equity |
|---|---|
| 2026-08-04 (trading capital) | 100,075 |
| 2026-09-24 (last review) | 99,676 |
| 2026-10-01 | 98,573 |
| 2026-10-31 | 97,173 |
| 2026-11-02 08:56 CT | 96,996 |

October cost about 1,400 of equity (1.4 percent). Since inception the account is down 3.1 percent.

### 2.2 Closed trades, October (closed_ts in October)

38 trades, 17 winners (45 percent), realized -1,692.85, -5.40 R.

| Week of | Trades | Winners | Realized |
|---|---|---|---|
| 2026-09-28 (incl. Oct 1 and 2) | 8 | 3 | -977 |
| 2026-10-05 | 10 | 5 | -22 |
| 2026-10-12 | 10 | 6 | +117 |
| 2026-10-19 | 6 | 2 | -313 |
| 2026-10-26 | 7 | 2 | -1,163 |

### 2.3 Lanes

October:

| Lane | Trades | Winners | Realized | Sum R |
|---|---|---|---|---|
| scanner short | 15 | 9 | +243 | +1.10 |
| news long | 4 | 3 | +274 | +0.56 |
| scanner long | 5 | 2 | -83 | -0.38 |
| news short | 9 | 3 | -988 | -2.03 |
| thesis long | 5 | 0 | -1,140 | -4.64 |

Since inception: scanner +1,648 over 44 trades (25 winners), news -2,330 over 51 trades (22 winners), thesis -1,509 over 10 closed trades (2 winners). The scanner remains the only lane that makes money. News shorts are the worst news sub lane this month; the OVERNIGHT layer closed 7 of them at a loss (see 2.4).

The thesis lane is the single largest drain and it concentrates: 12 entries ever, all from two theses. `th-2026-001` (AI infrastructure: RIOT, FRMI, RXT) bought RIOT four times at 21.43, 23.05, 19.15 and 13.89 while it fell from 23 to 12, and FRMI three times. Both open positions are thesis longs under water: INVX -227 (87 at 29.03, see 1.3) and RIOT -107 (67 at 13.89, catastrophe stop 8.49 in place). A9 did not flag the lane because its rule needs 10 trades in the four week window.

### 2.4 Exit layers, October

| Layer | Exits | Realized | Avg R |
|---|---|---|---|
| TRAIL | 9 | +1,658 | +0.83 |
| TARGET | 12 | +1,344 | +0.51 |
| BREAKEVEN | 1 | +76 | +0.16 |
| FORCE_FLAT | 1 | +54 | +0.22 |
| GUARD | 1 | 0 | 0 |
| TIME | 7 | -179 | -0.06 |
| REVIEW | 1 | -131 | -0.54 |
| OVERNIGHT | 7 | -735 | -0.21 |
| STOP | 11 | -3,780 | -0.91 |

Weekly `exit_efficiency:STOP` went -0.52, -6.33, -6.66, -23.84 across the four full weeks. That trend is what A9 proposal 2 is about. Three of the eleven stop outs were CRWV scanner shorts on consecutive days (2026-10-15, 10-16, 10-20) for -1,327 together.

### 2.5 Guard, gate, shadows, funnel

- Guard: 36 verdicts in October, 35 HOLD, 1 TIGHTEN_STOP, 4 EXIT recommendations; classified so far 2 SAVE, 6 SHAKEOUT, 28 NEUTRAL, 5 pending. Weekly `guard_save_rate` 0 to 0.09. The HOLD bias noted in September is unchanged.
- Gate pass rate 4 to 9 percent by week. Top vetoes: EH_LIQUIDITY 455, CREDIBILITY 277, SHORT_UNAVAILABLE 234, GATE_NO_CONFIRM 179, GATE_EXTENDED 83.
- Shadows against their bars (2026-10-31 A9 review): `drift_short` 0 of 60 would trades measured; `fade` 29 of 60 measured, +0.67 percent average, 51.7 percent winners (bar: 60 trades, +0.4 percent, 55 percent winners, so it is on track on return and short on winners); scanner early window n=27, with move -1.2 R, 13 winners, under the bar.
- Scanner funnel counterfactuals: concurrency cap 2 of 10 capped instances journaled, with move -0.9 R; analyst shorts on scanner up moves n=51, long beats short by 7.5 R, which A9 reports as under its 2 R bar (wording in the A9 email is ambiguous; worth reading the rule before acting).

### 2.6 A9 proposals and emails

A9 ran every Saturday (2026-10-03, 10-10, 10-17, 10-24, 10-31) and raised two proposals, both still `PROPOSED`:

1. Proposal 1 (2026-10-17): scanner scalp, drop the 50 percent scale out and let the full size ride the 1.5 ATR trail. Evidence: +743 over 36 scanner trades. Success metric `exit_efficiency:TRAIL >= 0.55` weekly for two weeks (baseline 0.42).
2. Proposal 2 (2026-10-31): test a 3.0 ATR(5m) initial stop on the scanner lane in shadow before changing k. Evidence: `exit_efficiency:STOP` -10.5, stop3 variant -530 versus base over 10 instances (so the wider stop looked worse, A9 wants the shadow to run).

Approve with `python -m a9_review.service --approve N --config-version <hash>` once Ian decides; nothing is applied automatically.

Emails: every weekday morning briefing and evening digest and every Saturday review was sent (62 messages since 2026-09-24, none failed). The "needs you" list has carried the same items since 2026-10-08: the eight GlobeNewswire rows, `backup_offsite`, and (in the Saturday mail) proposals 1 and 2. None was acted on, which is expected for an absence, but it means the digest cannot tell a new problem from a standing one; a "new since yesterday" marker would help.

## 3. Recommended order of work

1. Today, with Ian's go: re-place the INVX catastrophe stop at the broker (1.3), or close INVX.
2. v0.26.5: cancel confirmed before exit, retry on rejected re-placement, reconcile re-arms a missing catastrophe stop, journal cancel state; `c4-exec` startup retry on broker or DNS failure (1.1).
3. Decide on GlobeNewswire (1.2): re-probe, back off, or drop.
4. Thesis lane: at least one entry per ticker per thesis, or pause `th-2026-001`; consider pausing the lane until A9 has a rule for it (2.3).
5. Decide proposals 1 and 2 (2.6).
6. Google Drive token for `backup-offsite` (`docs/claude_backup-offsite-2026-09-22.md`).
7. Watch items: NVRM out of memory lines at the batch hours, analyst latency, the halt heuristic on thin names (quotes instead of bars would stop the INVX false freezes).
