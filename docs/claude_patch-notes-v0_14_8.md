# Patch notes v0.14.8

Config only. 2026-09-14. Previous tag: `v0.14.7`.

## Change

`config/shorting.yaml`: `shorting.mode` from `shadow` to `live`. Committed, with a comment recording why.

## Why

The operator noticed no short sales for a long time. Investigation (read only, 2026-09-14):

- Shorting went live on 2026-08-15 as an uncommitted local edit on the box. Real shorts were sized and filled through 08-27 (META 08-26, NTNX 08-27).
- On 2026-08-22 at 13:14 CT the drift was committed as `ad30ed4` ("v0.13.11: shorting mode live"), then at 13:17 CT a `git reset` to `origin/main` discarded that commit and wrote the file back to `mode: shadow` (file mtime 13:17:41 matches).
- A3 had restarted at 12:51 that day, before the reset, so it kept `live` in memory until the 08-27 17:35 CT reboot. From then on every short became a `SHADOW_SHORT` journal row and no order. 14 shadow shorts between 09-04 and 09-14, zero real shorts.
- Nothing else blocks shorts: all lanes on, account `shorting_enabled = 1`, ETB and SSR vetoes behaving normally, down theses reaching A3 and fully sized.

The `config_version` journaled on each decision is the git commit at service start and does not reflect local config edits, which is why the journal could not show the flip either way. A gotcha line is added to `CLAUDE.md`.

## Services restarted

`a3-risk`, `c3-gate`, `c4-exec` (the three readers of the shorting block, per the file's own note). Evening of 2026-09-14, after the close, watchdog timer paused around the restart. Verification and time recorded below once done.

## Verification

- All three `active`, journals show `config version active` for the v0.14.8 commit, no tracebacks.
- Next session: the first down thesis that passes the gate journals RISK `SIZE` with `side: SHORT` and a `SELL_SHORT` intent, not `SHADOW_SHORT`. The A3 log line reads `intent`, not `SHADOW short`.

## Rollback

Set `mode: shadow` in `config/shorting.yaml`, commit, restart the same three services. Or `git reset --hard v0.14.7` on `main` and restart the same three.

## Deploy record

Deployed 2026-09-14 15:59 CT (market closed) with operator go, bundled with v0.14.9: `c7-watchdog.timer` paused, `a2-analyst a3-risk a12-guard a13-chat c3-gate c4-exec` restarted, timer resumed. All six active, journals show `config version active 272536273a34` (the v0.14.9 commit, which includes this change), zero errors; `analyst`, `risk`, `guard`, `chat`, `gate`, `exec`, `deadman` heartbeats fresh within a minute. Shorting is live from the 2026-09-15 session.
