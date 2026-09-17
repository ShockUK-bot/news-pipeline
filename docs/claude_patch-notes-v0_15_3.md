# Patch notes v0.15.3 (2026-09-16, deployed 17:17 CT)

## What changed and why

The session 1 BURST scoreboard showed that the 1 percent target the C12 scoring used (from the "1 percent gain" idea) is the wrong number for liquid names: the move that is reliably there after a burst is 0.3 to 0.5 percent over 5 to 15 minutes, taken against the burst, not 1 percent with it. Rather than guess the right bracket from percentiles, C12 now scores every event against five target/stop brackets at the same time, so "what size of move is realistic" is measured on live data.

- `src/c12_burst/service.py`: when an event completes its 30 minute path, the same path is scored against five brackets and the result is stored in `detail.brackets` as `{"t0.5_s0.5": ["target", 3.2], ...}` (first hit and minutes to it). Brackets: 0.5/0.5, 0.7/0.5, 1.0/0.7, 1.0/1.0, 0.5/0.35 (target/stop, percent). Overridable with `score_brackets` under the detector block of the burst config. The existing `first_hit` columns (1.0/0.7) are unchanged.
- `ops/tools/burst_report.py`: prints a "Bracket sweep" table per rule, direction and bracket: n, target first, stop first, and expected value per trade assuming exits at the levels with the cost applied (`--cost-bps`, default 10).
- `pyproject.toml`, `CLAUDE.md`: version.

No migration: `detail` is already jsonb.

## Services restarted

`c12-burst` at 17:17 CT (after the close). Research only, no order path, so no market hours constraint.

## Verification

- Unit tests green before packaging.
- `c12-burst` active after the restart, stream healthy (stats lines every 10 minutes, no errors).
- Bracket data not yet verifiable: no burst fired after the restart (after hours is quiet). First check is the first scored rows tomorrow, from about 09:10 CT: `select detail->'brackets' from journal.burst_events where complete order by ts desc limit 5` should show five keys, and `burst_report.py --days 1` should print the sweep.
- Side effect noted: the restart dropped the 5 detections still pending their 30 minute path (14:00 hour rows with `complete=false`). Pending scoring is in memory, so a restart inside the 30 minute window loses it. Harmless for research, worth remembering when timing restarts.

## Rollback

`git checkout v0.15.2 -- src/c12_burst/service.py ops/tools/burst_report.py` then `sudo -n systemctl restart c12-burst`. Rows already written with `detail.brackets` are ignored by the older report.
