# C12 burst stream: retirement and what to reuse (2026-09-22)

## Verdict

Built 2026-09-15 (v0.15.0) as a research stream, no order path. Two sessions scored at the detection print looked promising for a fade; v0.15.4 added the realistic entry (first print 30 seconds after detection, half the spread paid) and three sessions on that column, 366 scored rows, were negative after cost in every cell:

| Rule | n | Realistic 30 min | After cost |
|---|---|---|---|
| Fade, short an up burst | 91 | +0.03% | −0.07% |
| Fade, buy a down burst | 88 | −0.11% | −0.21% |
| Momentum, with an up burst | 98 | −0.14% | −0.24% |
| Momentum, with a down burst | 89 | 0.00% | −0.10% |

Per session, every fade and momentum cell was flat or negative. The print based edge was the first minute snap back and nothing else. Retired 2026-09-22 (v0.25.0): `c12-burst` stopped and disabled, BURST tab and `/api/burst` removed from the dashboard, watchdog entry removed, A9 rule retired. Kept: the unit file (`ops/systemd/c12-burst.service`, not enabled), `config/burst.yaml`, `journal.burst_events` (all rows, research history), `ops/tools/burst_report.py`, and the code under `src/c12_burst/`. The unit tests for the code stay in the suite so the library keeps working.

## What is worth reusing for the scanner

The stream itself found no edge, but it solved three problems the scanner has, and the code is pure and tested:

1. **`src/c12_burst/book.py` `SymbolBook`**: a per symbol rolling book of 5 second buckets built from the live trade stream. Gives `ret(now, secs)`, `vol_mult(now, window, baseline)` (volume against the symbol's own median bucket volume, restricted to regular hours), `extreme(now, secs)` (new 30 minute high or low) and `price_at`. The scanner today polls a REST screener once a minute and estimates relative volume against a day pace, which is noisiest in the first minutes after the open, exactly where the scanner makes its money (10 of 15 winners in the 08:50 to 09:00 slot) and where SNDK ran 7 percent before the first scan.
2. **`src/c12_burst/detect.py` `evaluate`**: the pure detection rule (return thresholds, volume multiple, new extreme, per symbol cooldown). Threshold values are config.
3. **`src/c12_burst/service.py`**: the Alpaca SIP websocket client with reconnect and backoff, universe validation, and the heartbeat pattern. About 150 liquid names at under 1 percent CPU.

## The build that uses them: scanner fast lane (proposed, not built)

- A `scanner_stream` mode in C10 (or a slim C12 relaunch as a feed, no journal of its own): the book runs from 08:30 CT on the scanner's universe (movers and most actives from the previous close plus the static liquid list), and from `early_window.start_et` a symbol that is up or down at least `min_move_pct` on the day on a `vol_mult` above the bar with a new 30 minute extreme is handed to the scanner's normal candidate path within seconds, bypassing the screener poll.
- Everything after the handoff is unchanged: metrics, score, dispositions, analyst, gate, A3, the concurrency cap. So the fast lane only changes WHEN the scanner sees a name, not what it does with it.
- Gate it on evidence: the early window shadow (v0.24.0) journals every 09:33 to 09:50 ET candidate and A11 replays it; A9 proposes opening the window at 20 replays summing to +3R with half winners. Build the fast lane when that proposal is approved, since only then does seeing a name 40 seconds sooner pay.
- Estimated size: one evening. The stream client, the book and the detector already exist; the new code is the handoff and the config.
