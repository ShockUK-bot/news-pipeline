# Patch notes v0.15.0

C12 burst stream: a real time, research only burst detector. Built and deployed overnight 2026-09-15 to 16. Previous tag: `v0.14.13`. Design and evidence: `docs/claude_1pct-gain-design-2026-09-15.md`; build log: `docs/claude_c12-build-handoff-2026-09-15.md`.

## What it is

The "1 percent gain" idea failed every test on the data the system had (60 second polls of daily movers, 1 minute bars). The missing ingredient was seeing a burst within seconds of it starting, in liquid names, with a live spread. C12 supplies that data and measures the candidate rules for two weeks before any lane is built. **It places no orders and enqueues nothing**; a unit test pins that the service contains no order or queue call.

## Components

- `src/c12_burst/stream` (inside `service.py`): Alpaca data websocket, `wss://stream.data.alpaca.markets/v2/sip` (feed from `ALPACA_FEED`, automatic IEX fallback on a subscription error), auth, subscribe to `trades` and `bars` for the universe, reconnect with backoff, same pattern as the news stream.
- `src/c12_burst/book.py`: per symbol rolling 5 second buckets (45 minutes), pure. Returns, volume multiple against a median baseline (RTH only), 30 minute extremes, and the forward path scoring.
- `src/c12_burst/detect.py`: rule `momentum`: |60 s return| at or above 0.4 percent or |120 s return| at or above 0.6 percent, 60 s volume at or above 4x the baseline pace, price at a new 30 minute extreme in the move direction, session 08:36 to 14:55 CT, 15 minute cooldown per symbol, symbols with no print in 30 s skipped.
- Each detection journals up to three rows in `journal.burst_events`: `momentum` (burst direction), `fade` (opposite direction, same detection) and `news_anchored` (only when an A1 escalation for the symbol exists in the last 15 minutes; the news direction hint is stored). `scanner_known` marks symbols the scanner already flagged that day; `spread_bps` comes from a one off snapshot at detection.
- Forward path filled in process at +30 minutes from the book (no REST sweep): prices at +1, +5, +15, +30 minutes, max favourable and adverse excursion, and which of a 1 percent target or 0.7 percent stop came first.
- Universe: journal derived liquid names (ADV over 1bn seen by the scanner or the gate in 30 days) plus a static large cap list, validated against the assets API at startup, capped at 400. 148 symbols on the first run.
- `schema/migrations/016-burst-events.sql` (applied 2026-09-15 evening, `dash_reader` granted select). `config/burst.yaml`. `ops/systemd/c12-burst.service` (long running, enabled). `config/watchdog.yaml` entry `c12-burst`, health `burst`, 10 minute freshness. `ops/tools/burst_report.py` for reading results.

## Tests

`tests/unit/test_v0_15_0.py`, 14 tests: bucket roll, returns and extremes, volume multiple, forward path scoring (target and stop), momentum long and short, volume floor, extreme requirement, cooldown, staleness, session window, timestamp parsing, config pins, no order path. Suite: 840 passed, 1 skipped.

## Deploy record

2026-09-15 about 23:30 CT: migration 016 applied; foreground smoke run (148 symbols validated, SIP subscribed, heartbeat, clean shutdown on SIGTERM); unit installed, enabled and started; `c12-burst` active, `burst` heartbeat OK ("sip stream, 148 symbols"), zero errors; about 620 MB resident, under 1 percent CPU off session. First live session 2026-09-16 (detection starts 08:36 CT).

## How to watch it

- `journalctl -u c12-burst -f` shows a `burst` line per detection with symbol, direction, returns, volume multiple, spread, news and scanner flags, and a `stats` line every 10 minutes (trades, events, pending).
- `ops/tools/burst_report.py` (env sourced) prints today's momentum detections with their outcomes once scored (30 minutes after each) and the per rule scoreboard against the go live bar: 200 or more scored events, EV at or above +0.15 percent per trade after a 10 bps cost, no more than 40 percent of sessions negative.
- SQL: `select * from journal.burst_events order by ts desc limit 20;`

## Expectations for the first session

Unknown event rate; the 1 minute backtest suggested 7 to 17 bursts a day on 83 names with looser rules, so roughly 10 to 40 detections across 148 names is plausible. If the count is very high, raise `vol_mult` or `ret_60s`; if near zero, check `stats` trade counts first (a silent stream means a feed problem, not a quiet market). Tuning is config only; the service reads `burst.yaml` at startup, so a restart applies it (any time, C12 touches nothing live).

## Rollback

`sudo -n systemctl stop c12-burst && sudo -n systemctl disable c12-burst`; remove the `c12-burst` entry from `config/watchdog.yaml` (or the watchdog will alert on the missing heartbeat). The table can stay; nothing else reads it. Repo: `git reset --hard v0.14.13`.

## Next (not in this release)

After two weeks: score the three rules with `burst_report.py --days 10`. If one clears the bar, build the lane: C12 to A3 directly (code only sizing), a `snipe_v1` profile with a full exit at target (one addition to the exit ladder), 10 to 15 minute time stop, force flat. If none clears it, the stream still gives C4 second resolution stops and the scanner a live universe.
