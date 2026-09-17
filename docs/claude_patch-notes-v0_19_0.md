# Patch notes v0.19.0 (2026-09-17, 17:22 to 17:30 CT): sector data source

The sector heat clip in A3 and the analyst's `sector` context slot had never had data (every sized trade carried `SECTOR_UNKNOWN`). This release fills both from SEC SIC codes, with no new provider or key.

## How it works

- `src/common/sectors.py`: a static SIC range table maps to eleven sectors (`sector_for_sic`), carve outs first (pharma 2833 to 2836, medical devices, defense electronics 3812, aircraft 3720s, REITs 6798, semiconductors and comms equipment 3660 to 3699, electrical equipment 3600 to 3629 as Industrials, oil and gas field machinery 3533 as Energy, and so on). The ticker's CIK comes from the cached SEC company list the EDGAR poller already keeps (`var/cik_map.json`); the SIC comes from the SEC submissions record for that CIK, fetched with the pipeline's registered EDGAR User-Agent under the fair access rate.
- `journal.sectors` (migration 019, applied 17:22 CT): one row per ticker with cik, sic, description, sector, name, source, updated_ts. Unknowns are stored too (`no_cik`, or a CIK without a SIC) so a miss is not refetched on every sizing call. Rows refresh after 90 days.
- `sector-map.timer` (04:40 CT daily, before the premarket runs): maps every ticker touched in the last 7 days (positions, scanner candidates, gate, risk, analyst and thesis decisions, the burst universe) that has no row or a stale one. Heartbeat `sectors`.
- **A3**: at sizing, `sector` is looked up (one bounded EDGAR fetch on a miss) and `sector_heat` is the open risk dollars of open positions in the same sector, so the 1.5 percent `max_sector_heat_pct` clip is live for the first time. Unknown sectors keep the flag path.
- **A2**: the analyst context's `sector` is filled from the store (lookup only, no fetch on that path).
- CLI: `python -m common.sectors --refresh [--days N] [--dry-run]`, `--lookup TICKER`, `--remap` (recompute sectors from stored SIC codes after a table change, no network).

## Backfill, 17:22 CT

1,024 tickers: 923 mapped, 57 without a US filer record (foreign issuers and ADRs stay unknown), 44 with a CIK but no SIC (mostly SPACs and funds). Zero fetch failures. Spread: Health Care 192, Information Technology 184, Industrials 154, Financials 132, Consumer Discretionary 65, Materials 50, Real Estate 44, Energy 31, Communication Services 28, Consumer Staples 23, Utilities 20. Open positions: RIOT Financials, INVX Energy, FRMI Real Estate. All eight scanner trades since 09-14 map (seven Information Technology, CRCL Financials), which is the cluster the sector clip is meant to catch.

## Services

`a3-risk` and `a2-analyst` restarted 17:22 CT (after the close). `sector-map.timer` installed and enabled. `config/watchdog.yaml` gained the timer and the `sectors` heartbeat. Suite 873 passed.

## Rollback

`git reset --hard v0.18.0` on main and restart `a3-risk` and `a2-analyst`. The table and timer can stay.

## Follow ups

- The scanner sector cluster rule (one scanner position per sector at a time, or a sector cap) can now be built; today's four capped AI semis were all Information Technology.
- The 44 CIK rows without a SIC could be filled from the company's latest 10-K header in a later release.
