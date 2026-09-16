# Patch notes v0.15.1

Dashboard BURST tab. 2026-09-16, deployed 08:45 CT during the session (`c6-dashboard` is not a protected unit; operator asked for a daytime deploy). Previous tag: `v0.15.0`.

## Change

- `dashboard/app.py`: `GET /api/burst` (read only): the `burst` heartbeat, today's momentum detections newest first with their scores once complete, today's counts, and the per rule and direction scoreboard over the last N days (default 5).
- `dashboard/index.html`: BURST tab, refreshed every 10 seconds while open. Top panel: stream status, today's counts, one row per detection (time, symbol, direction, price, 60 s and 120 s move, volume multiple, spread, news and scanner flags, and once scored: target or stop first and when, best and worst excursion, 30 minute return). Bottom panel: scoreboard by rule (momentum, fade, news_anchored) and direction with the go live bar in the caption.

## Verification

`c6-dashboard` restarted 08:45 CT, active, zero errors. `/api/burst` returned the live heartbeat ("sip stream, 148 symbols") and 6 detections from the first 15 minutes of the session; the page serves the tab.

## Rollback

`git reset --hard v0.15.0`, restart `c6-dashboard`.
