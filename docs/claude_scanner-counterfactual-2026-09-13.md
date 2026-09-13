# Scanner counterfactuals and stop slippage, 2026-09-13

Follow up to `claude_scanner-review-2026-09-13.md`. Read only. No config changed, nothing restarted. All times America/Chicago.

## Data note: there are no journaled 5 minute bars

Nothing in the database or on disk stores bars: `journal.counterfactuals` is empty, `journal.trade_metrics` is empty, and there is no bars table. The live C4 exit engine (`src/c4_exec/service.py`, `engine_loop`) pulls the last 1 minute bar from Alpaca every 60 seconds and evaluates the ladder on it. So for Part A I pulled the same 1 minute bars from Alpaca's historical data API (read only, `common.marketdata.AlpacaData.minute_bars`, SIP feed, 391 bars per ticker day, 5,083 bars total) and replayed the `scalp_v1` ladder on them. That is the same bar cadence production uses, so it is closer to the truth than a 5 minute replay would be.

The replay mirrors `src/c4_exec/exits.py` exactly: stop on bar low (long) or high (short), then time stop (60 minutes, under 0.5R), then 50 percent scale out at entry x (1 + 0.6 x magnitude_est), then breakeven at 0.75R and a 1.5 x ATR(5m) trail from 1R, tighten only, force flat 14:50. Fills are assumed at the stop price, target price, or bar close. The ATR(5m) and magnitude_est are the journaled values from the live position (or the shadow decision).

**Validation.** Replaying the six real trades with their real entries reproduces every exit layer and every exit time within one minute of the journal (KLAC stop 09:26 vs 09:27 real, SKHY time stop 10:00 vs 09:59, SPCX 09:05 vs 09:06, DELL scale out then trail exit at 09:24 vs 09:25 at 562.36 vs 562.33). The R differences between "replay" and "actual" below are stop slippage, which Part B measures directly.

## Part A: alternatives for the six scanner longs

R is measured against each alternative's own R unit (3.0 ATR makes the unit 1.5x bigger). Dollars use the actual share count; for the 3.0 ATR case the risk constant figure (two thirds of the shares, same dollar risk) is in brackets.

| Ticker | Date | Actual | Replay (2.0 ATR) | 3.0 ATR stop, same entry | Enter 09:15 | Enter 09:30 |
|---|---|---|---|---|---|---|
| KLAC | 09-04 | -0.78R, -105 | -1.00R, -135, STOP 09:26 | -0.14R, -28 [-19], TIME 09:59 | -1.00R, -135, STOP 09:26 (px 185.33) | +0.99R, +133, TARGET 10:11 then TRAIL 10:22 (px 184.55) |
| SKHY | 09-08 | -0.09R, -11 | -0.09R, -12, TIME 10:00 | -0.06R, -12 [-8], TIME 10:00 | +0.86R, +115, TARGET 10:13 then TRAIL 10:25 (px 187.24) | +0.10R, +13, TIME 10:30 (px 188.53) |
| SKHY | 09-09 | -1.08R, -145 | -1.00R, -134, STOP 09:21 | -0.12R, -24 [-16], TIME 09:53 | -1.00R, -134, STOP 09:22 (px 195.14) | +0.48R, +65, TARGET 10:00 then BREAKEVEN 10:10 (px 194.43) |
| SPCX | 09-10 | -1.62R, -213 | -1.00R, -131, STOP 09:05 | -1.00R, -196 [-131], STOP 09:05 | +0.35R, +46, TIME 10:15 (px 149.93) | -0.24R, -31, TIME 10:30 (px 150.03) |
| AVAV | 09-10 | -0.75R, -119 | -1.00R, -158, STOP 09:28 | -1.00R, -238 [-158], STOP 09:28 | n/a, detected 09:26 | -1.00R, -158, STOP 09:30 (px 157.43) |
| DELL | 09-11 | +1.16R, +216 | +1.11R, +208, TARGET 08:59 then TRAIL 09:24 | +0.74R, +208 [+138], same exits | +0.59R, +110, TARGET 09:20 then TRAIL 09:24 (px 558.91) | -1.00R, -187, STOP 10:14 (px 564.57) |
| **Total** | | **-3.16R, -378** | **-2.99R, -364** | **-1.57R, -290 [-193]** | **-0.21R, +2** (5 trades) | **-0.67R, -165** |

AVAV's 09:15 row is excluded from the total because the scanner had not detected it yet (first candidate row 09:26:08), so that entry would be look ahead.

What the longs say:
- **3.0 ATR stop.** Converts the three chop stop outs (KLAC, SKHY, SKHY) into small time stop losses, but SPCX and AVAV still stop out and cost 50 percent more per share. DELL earns the same dollars at a lower R. Net improvement is real but modest (-1.57R against -2.99R, or -193 against -364 at constant risk), and it comes entirely from the three cases where price recovered within 60 minutes. It would not have rescued the two genuine reversals.
- **09:15 entry.** Roughly flat instead of -3R: SKHY 09-08 and SPCX become winners, DELL keeps a smaller win, KLAC and SKHY 09-09 still stop out. Note that a 09:15 SPCX entry is at 149.93, 2.8 percent below the real 154.23 entry, so the "same candidate" is a very different trade by then.
- **09:30 entry.** Worse than 09:15 and DELL flips to a full loss (entered at the top at 564.57). KLAC becomes a clean win. Inconsistent.

## Part A: the seven shadow shorts

Shadow shorts are journaled at RISK as `SHADOW_SHORT` with a limit price, quantity, stop and exit policy but are never sent to the broker. The replay assumes a fill at the limit price at the decision time. Nothing to validate against, so treat these as paper.

| Ticker | Date | Entry (time) | Replay (2.0 ATR) | 3.0 ATR stop | Enter 09:15 | Enter 09:30 |
|---|---|---|---|---|---|---|
| IOT | 09-04 | 41.97 (08:53) | +1.53R, +277, TARGET 08:53 then TRAIL 08:58 | +1.02R, +277 [+185] | -1.00R, -182, STOP 09:21 (px 40.19) | +1.08R, +196 (px 40.84) |
| TSLA | 09-04 | 358.07 (08:55) | +1.08R, +151, TARGET 09:19 then TRAIL 10:01 | +0.72R, +151 [+101] | 0.00R, 0, BREAKEVEN 10:03 (px 355.53) | 0.00R, 0, TIME 10:30 (px 353.26) |
| RGTI | 09-08 | 16.42 (08:52) | +1.16R, +241, TARGET 09:12 then TRAIL 09:47 | +0.77R, +241 [+160] | +0.71R, +148 (px 16.21) | +0.41R, +84 (px 16.14) |
| VRT | 09-10 | 245.33 (08:53) | -1.00R, -181, STOP 09:00 | -1.00R, -272 [-181] | -1.00R, -181, STOP 09:24 (px 248.21) | 0.00R, 0, BREAKEVEN 10:13 (px 252.23) |
| CIFR | 09-10 | 15.33 (08:54) | -1.01R, -247, STOP 09:01 | -1.00R, -367 [-245] | -1.01R, -247, STOP 09:20 (px 15.51) | +0.40R, +97, TIME 10:30 (px 16.23) |
| SMR | 09-11 | 9.21 (08:54) | -0.98R, -237, STOP 08:59 | +0.36R, +133 [+88], TIME 09:54 | +1.20R, +292 (px 9.39) | +0.74R, +179 (px 9.26) |
| OKLO | 09-11 | 37.11 (08:55) | -0.99R, -241, STOP 08:59 | -1.00R, -364 [-243] | +0.94R, +228 (px 37.80) | +0.86R, +208 (px 37.70) |
| **Total** | | | **-0.22R, -237** | **-0.13R, -201 [-134]** | **-0.16R, +57** | **+3.48R, +765** |

What the shorts say:
- At the real entry time the shorts are three winners and four losers, about flat in R and -237 in dollars. The three winners were fading immediately; the four losers bounced within 5 to 7 minutes of the 08:52 to 08:55 entries and stopped out.
- The 09:30 entry is the only alternative in either lane with no losing trade (three wins, two flat, two small wins). Seven paper trades in one week is not evidence of anything on its own, but the shape is consistent with the long side story: the first 10 minutes of the scanner session are where the 2 ATR(5m) stop gets hit.
- The delayed entries assume the gate would still pass (run since detect, spread) and A3 would still size the trade at 09:15 or 09:30. Live, some of these would be vetoed on the re check. They also assume borrow was still available at the later time.

Across both lanes the consistent finding is entry timing, not stop width: every alternative that moves the entry out of the 08:50 to 09:00 window removes most of the stop outs, whereas widening the stop only trades quick stop outs for slower, larger ones.

## Part B: stop slippage since 2026-08-15, both lanes

Every STOP, BREAKEVEN, TRAIL or CATASTROPHE exit in `journal.exits` since 08-15 (twelve). "Stop at exit" is the last ratcheted stop before the fill (or the initial stop). Slippage is measured from that stop to the fill: positive means the fill was worse than the stop, negative means better. Negative values are normal: the engine sells at the bid when a bar's low touches the stop, so a wick through the stop often fills above it.

| Date | Ticker | Lane | Layer | Exit time | Stop at exit | Fill | Slip (cents) | Slip (R) | ADV20 $M |
|---|---|---|---|---|---|---|---|---|---|
| 08-19 | SAN | news | STOP | 10:36 | 14.11 | 14.18 | -7 | -0.16 | 185 |
| 08-26 | INTU | scanner | BREAKEVEN | 09:39 | 342.73 | 342.50 | +23 | +0.07 | 1,263 |
| 08-27 | CRM | scanner | TRAIL | 09:28 | 248.15 | 248.21 | -6 | -0.03 | 2,876 |
| 08-28 | CRWD | scanner | BREAKEVEN | 08:37 | 219.55 | 218.21 | **+134** | **+0.54** | 1,721 |
| 09-01 | FRMI | thesis | STOP | 08:38 | 4.83 | 4.77 | +6 | +0.03 | 88 |
| 09-04 | KLAC | scanner | STOP | 09:27 | 183.69 | 184.07 | -38 | -0.22 | 1,574 |
| 09-08 | AZN | news | TRAIL | 11:09 | 160.16 | 160.07 | +9 | +0.01 | 614 |
| 09-09 | BW | thesis | STOP | 08:34 | 7.70 (tightened 09-08, thesis invalidated) | 7.75 | -5 | -0.01 | 38 |
| 09-09 | SKHY | scanner | STOP | 09:22 | 193.49 | 193.34 | +15 | +0.08 | 3,519 |
| 09-10 | SPCX | scanner | STOP | 09:06 | 152.83 | 151.97 | **+87** | **+0.62** | 11,955 |
| 09-10 | AVAV | scanner | STOP | 09:30 | 156.86 | 157.29 | -43 | -0.25 | 254 |
| 09-11 | DELL | scanner | TRAIL | 09:25 | 562.36 | 562.33 | +3 | +0.00 | 4,532 |

Mean slippage across the twelve is +0.06R, median about zero. Ten of twelve are within a quarter R either way. **SPCX is not a one off: CRWD on 08-28 slipped 0.54R through a breakeven stop.** The two outliers share a profile: large cap scanner longs (1.7bn and 12bn ADV) exited in the first 40 minutes of the session (08:37 and 09:06), where a single 1 minute bar can travel more than the whole 2 ATR(5m) stop distance. Liquidity is not the problem; bar range against a stop distance of about 1 percent is. That is the same first hour pattern as Part A.

Slippage is currently not journaled anywhere; this table was reconstructed by joining exits to the last stop event. Item 3 below fixes that.

## v0.14.6 scope (three items, no config changes, not implemented)

### 1. Freshness credit for candidates at their day extreme

- **Problem.** `src/c10_scanner/rules.py` line 276: `fresh = 1.0 - min(m.minutes_since_extreme or 60, 60) / 60.0`. A candidate 0 minutes from its high or low is falsy, gets 60 substituted, and scores zero freshness instead of full. AVAV on 09-10 was journaled at 0.6236 instead of about 0.77.
- **Change.** Treat only `None` as unknown: `mins = 60 if m.minutes_since_extreme is None else m.minutes_since_extreme`.
- **Files.** `src/c10_scanner/rules.py` (one line). `tests/unit/test_scanner.py`: two cases, `minutes_since_extreme == 0` gives the full 0.15 credit, `None` gives zero, plus a regression assertion that AVAV's 09-10 metrics score about 0.77.
- **Service restart.** `c10-scanner`.

### 2. Pre close evaluation of `close_below_prenews` at 14:55 CT

- **Problem.** Session timeframe predicates only run in `engine.session_close_pass` after 16:01 ET, when the exit order cannot fill; the position is then carried to the next day's 14:45 overnight exit. All four news positions this week went that way.
- **Change.** In `engine_loop` (`src/c4_exec/service.py`), add a once per day pass at 15:55 ET (14:55 CT), run before the 15:55 overnight pass: for each open position that has session timeframe predicates, build a provisional session bar (today's open, high, low from the minute bars already fetched, close = last price) and call `engine.step(pos, {**bar, "tf": "session", "provisional": True})`. If the predicate fires, the normal INVALIDATION exit executes while the market is open. Keep the 16:01 pass unchanged as confirmation; on an already closed position it is a no op, and on a position that dipped at 14:55 but was not exited (fill failed) it fires again as today.
- **Design points to confirm with you before building.** (a) A dip below the pre news price at 14:55 that recovers by 15:00 will now exit where the close pass would not have; that is the intended trade. (b) The pass is code default 15:55 ET; if you later want it tunable I would add `invalidation_preclose_et` to `config/exit_profiles.yaml` in a separate config change, not in this release. (c) Scalp positions are already force flat at 15:50 ET, so this only affects news and thesis lanes.
- **Files.** `src/c4_exec/service.py` (engine_loop, new pass and its once per day guard), `src/c4_exec/engine.py` (`preclose_invalidation_pass`, sharing the bar to session bar conversion with `session_close_pass`), possibly `src/common/invalidation_dsl.py` if the session monitor needs to accept a provisional bar without changing its persist rule (persist is already 1 for session predicates). `tests/unit/test_exit_engine.py` or `test_invalidation_dsl.py`: provisional bar below prenews fires INVALIDATION; above does not; the 16:01 pass after a filled pre close exit is a no op; the pre close pass never runs twice in a day.
- **Service restart.** `c4-exec`.

### 3. Journal stop slippage on the exit event

- **Problem.** The exit event records only layer, qty, price and pnl. The stop level that triggered the exit lives only in the `ExitAction.reason` string, so slippage has to be reconstructed as in Part B.
- **Change.** Add `trigger_price` to `ExitAction` (`src/c4_exec/exits.py`; the stop for STOP/BREAKEVEN/TRAIL, the target for TARGET, the bar close for TIME and FORCE_FLAT). Pass it through `engine._apply` and `mechanics.execute_exit` to `state.record_exit`, which adds `trigger_price`, `slip_px` (signed, positive is worse) and `slip_r` to the `new_value` of the EXIT and SCALE_OUT position events. No migration: `new_value` is jsonb. The CATASTROPHE path in mechanics records the broker stop price as the trigger.
- **Files.** `src/c4_exec/exits.py`, `src/c4_exec/engine.py`, `src/c4_exec/mechanics.py`, `src/c4_exec/state.py`. `tests/unit/test_scalp_exits.py` and `test_exit_engine.py`: each exit layer sets `trigger_price`; `record_exit` writes `slip_px` and `slip_r` with the right sign for a long and a short (FakeBroker fill above and below the stop).
- **Service restart.** `c4-exec`.

### Release mechanics

- Bump `pyproject.toml` to 0.14.6, run the full unit suite, tag `v0.14.6`, write `docs/claude_patch-notes-v0_14_6.md`.
- Deploy window: evening. Stop `c7-watchdog.timer`, restart `c10-scanner` and `c4-exec`, start the timer, verify `is-active`, tail both journals, check `scanner` and `exec` heartbeats.
- Rollback: `git checkout v0.14.6^` is not enough on this repo (stay on main); instead `git revert` the release commit or `git reset --hard v0.14.5` on main, then restart the same two services.

## Reproducibility

The replay script and the fetched bars are in this session's scratchpad (`sim.py`, `bars.json`); they were not committed. If you want the replay kept, say so and I will add it under `ops/tools/scalp_replay.py` in the v0.14.6 release so future reviews can rerun it against any position id.
