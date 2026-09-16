# C12 burst stream: build handoff (v0.15.0), started 2026-09-15 late evening

Operator instruction (2026-09-15, before going to bed): scope C12 as v0.15.0 and build it so it is running in the 2026-09-16 trading window. If a subscription is missing, build with the assumption he will add it. If the build cannot finish in one session, leave this document so a new session can continue. Approved development items include their deploy (standing instruction, see memory `feedback-approved-item-includes-deploy`); market hours restart rules still apply, but C12 is a new unit and touches nothing live.

Design source: `docs/claude_1pct-gain-design-2026-09-15.md` (section "Recommended build"). Research first, order path second. **C12 v0.15.0 places no orders and enqueues nothing.**

## Scope (v0.15.0)

1. `src/c12_burst/` new package:
   - `stream.py`: Alpaca data websocket client (`wss://stream.data.alpaca.markets/v2/{feed}`, feed from `ALPACA_FEED`, sip default, iex fallback on an "insufficient subscription" error frame). Auth, subscribe to `trades` and `bars` for the universe, reconnect with backoff (same pattern as `c1_ingestion/sources/alpaca_ws.py`).
   - `book.py`: pure in memory state per symbol: rolling 5 second buckets (open, high, low, close, volume, trade count) for the last 45 minutes, 1 minute bars from the stream, baseline volume per 5 s bucket from the prior 30 minutes.
   - `detect.py`: pure detector rules over the buckets, evaluated every 5 seconds per symbol during 08:32 to 14:55 CT:
     - `momentum`: 60 s return at or above `ret_60s` (0.4 percent) or 120 s return at or above `ret_120s` (0.6 percent), bucket volume at or above `vol_mult` (4x) the baseline, price at a new 30 minute extreme in the move direction.
     - `fade`: the same burst, journaled with `direction` opposite (the fade thesis), same event, flag `rule='fade'` written as a second row so both can be scored.
     - `news_anchored`: momentum event where `journal.decisions` has a TRIAGE ESCALATE for the symbol in the last 15 minutes (direction hint recorded).
     - Cooldown per symbol per rule: 15 minutes.
   - `service.py`: wires stream, book, detector; writes `journal.burst_events`; fills the forward path in process (the book holds 45 minutes of history, so at +30 minutes the row is completed from memory, no REST sweep); heartbeat component `burst`; spread at detection from the last quote of a one off REST snapshot (`common.marketdata.AlpacaData.snapshot`).
2. `schema/migrations/016-burst-events.sql`: `journal.burst_events` (event_id, symbol, ts, rule, direction, price, ret_60s, ret_120s, vol_mult, spread_bps, news_anchored, news_direction, scanner_known, p_1m, p_5m, p_15m, p_30m, max_fav_pct, max_adv_pct, first_hit_1v07 text, first_hit_min numeric, complete boolean, created_ts). Additive only. Grant select to `dash_reader`.
3. `config/burst.yaml`: universe (journal derived liquid names plus a static large cap list), thresholds, cooldown, session window, feed.
4. `ops/systemd/c12-burst.service`: long running, like c10. `config/watchdog.yaml`: `c12-burst` entry, `health: burst`, `max_age_min: 10`. `CLAUDE.md` services line updated.
5. Tests `tests/unit/test_v0_15_0.py`: book bucket arithmetic, detector rules (momentum long and short, volume floor, extreme requirement, cooldown, session window), forward path completion, config pins.
6. Deploy: apply migration 016 on live Postgres, `sudo cp` unit, `daemon-reload`, `enable`, `start`, verify `burst` heartbeat and a `subscribed` log line. Tag `v0.15.0`, patch notes `docs/claude_patch-notes-v0_15_0.md`, current state file.

Go live rule for a later lane (not this release): a rule with 200 or more events, EV after measured cost at least +0.15 percent per trade, no more than 40 percent of sessions negative.

## Status log (append as work proceeds)

- 22:40 CT: scope written. Next: websocket auth test, then code.
- 22:50 CT: SIP websocket auth and subscription confirmed (IEX also works). No new subscription needed.
- 23:15 CT: code, migration, config, unit, tests written. Suite 840 passed.
- 23:25 CT: migration 016 applied on live Postgres. Foreground smoke: 148 symbols, subscribed, heartbeat, clean shutdown.
- 23:30 CT: `c12-burst` installed, enabled, started. Active, heartbeat OK, zero errors. Watchdog entry added.
- **BUILD COMPLETE.** Tagged `v0.15.0`. Nothing left for a new session except the morning check: `journalctl -u c12-burst --since 08:30` for `burst` lines and `ops/tools/burst_report.py` after 09:10 CT (first scored rows land 30 minutes after the first detection). See `docs/claude_patch-notes-v0_15_0.md`.
