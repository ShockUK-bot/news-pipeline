# Patch notes v0.20.0 (2026-09-17, 21:25 CT): sector policy, scanner cluster rule in shadow, sectors on the dashboard

Operator direction (2026-09-17 evening): 1.5 percent per sector is too low for a technology heavy tape, pair trades must not be double counted, and the scanner lane must not lose size. All three are in this release.

## 1. Sector heat policy (`config/risk.yaml`)

- `capital.max_sector_heat_pct` 0.015 to **0.03**. This is risk dollars (shares times stop distance), not notional: 3,000 dollars of open stop risk per sector on a 100k account.
- `capital.sector_heat_mode: net` (new): sector heat is the absolute difference of long and short risk in the sector, so a long and a short in the same sector offset. `gross` restores the v0.19.0 sum. Both numbers are journaled on every sized trade (`sizing.sector_heat_breakdown`).
- `scanner.sector_clip: false` (new): the scanner lane is not clipped by sector heat at all. Its open positions still count toward the heat the news and thesis lanes see. `src/a3_risk/sizing.py` skips the clip when the lane policy sets the cap to None; `sector` and `sector_heat` are always in the sizing numbers.

## 2. Scanner sector cluster rule, shadow mode

`scanner.sector_cluster: {mode: shadow, max_per_sector: 1}`. When a scanner entry arrives and the sector already holds that many open scanner positions, A3 journals `SCANNER_SECTOR_CLUSTER` in the RISK payload flags and sizes the trade anyway. `mode: veto` makes it a `SCANNER_SECTOR_CLUSTER` veto; `mode: off` disables it. Shadow first so A11 and A9 can measure what clustered entries actually do before anything is blocked (today's four capped AI semis would all have carried the flag). `cluster_verdict()` in `sizing.py`.

## 3. Dashboard

- Open positions and closed trades tables gain a Sector column (`journal.sectors` join, read only).
- New "Sector exposure" panel on the live tab: per sector, positions, tickers (shorts marked), long risk, short risk, net, gross, notional, and percent of the sector cap, with the policy line (cap in dollars, mode, scanner exempt, cluster mode) read from `config/risk.yaml` at startup.

## Tests and services

`tests/unit/test_v0_20_0.py` (clip binds only with a cap, scanner exemption, unknown still flags, net heat offsets a pair, cluster modes, yaml pins, dashboard shapes). Suite 878 passed. Dashboard queries exercised on live data before restart. Restarted `a3-risk` and `c6-dashboard` at 21:24 CT.

## Rollback

`config/risk.yaml`: `max_sector_heat_pct: 0.015`, delete `sector_heat_mode`, `sector_clip` and `sector_cluster`; restart `a3-risk`. Or `git reset --hard v0.19.0` on main and restart `a3-risk` and `c6-dashboard`.
