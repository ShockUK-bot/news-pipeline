# Patch notes v0.15.4 (2026-09-17, deployed after the close)

## What changed and why

Sessions 1 and 2 of the burst stream showed that most fade "wins" landed inside the first minute after detection: the detection print is the spike itself, so scoring from that print flatters the fade. From this release every event is scored twice: as before (from the print), and from a realistic entry.

- `src/c12_burst/book.py`: `entry_after(t0, delay)` returns the first print at or after detection plus the delay.
- `src/c12_burst/service.py`: at fill time the event is also scored from that print, with half the quoted spread added as crossing cost (a long pays up, a short sells down), over the remaining horizon, on the 1.0/0.7 bracket and all five sweep brackets. Stored in `detail.realistic` (`entry_px`, `print_px`, `entry_ts`, `half_spread_bps`, `ret_30m`, `max_fav_pct`, `max_adv_pct`, `first_hit`, `first_hit_min`, `brackets`). At detection time two flags are journaled in `detail`: `repeat` (a prior burst in the same name inside `repeat_window_secs`, with `prior_burst_secs`) and `fade_oversize` (burst larger than `fade_max_burst_pct`). Both populations lost in the measured sessions.
- `config/burst.yaml` `detect`: `entry_delay_secs: 30`, `repeat_window_secs: 1800`, `fade_max_burst_pct: 0.015`.
- `ops/tools/burst_report.py`: new "Realistic entry" scoreboard (average 30 minute return after cost, 0.5/0.5 and 1.0/0.7 hit rates, negative sessions, go live bar) over the sample without repeat bursts and without oversize fades. This is the column the go live decision is judged on from now.
- `tests/unit/test_v0_15_4.py`: four tests (entry after delay, the first minute snap back disappears from the realistic path, service level scoring with flags, yaml pins).

No migration (`detail` is jsonb). Rows scored before this release have no `realistic` block and are simply absent from the new table.

## Services restarted

`c12-burst` (research only, no order path).

## Verification

Unit suite 844 passed. Report run against live Postgres before restart: clean. After restart: unit active, heartbeat, no errors. First realistic rows appear from about 09:10 CT on 2026-09-18.

## Rollback

`git checkout v0.15.3 -- src/c12_burst config/burst.yaml ops/tools/burst_report.py` then `sudo -n systemctl restart c12-burst`.
