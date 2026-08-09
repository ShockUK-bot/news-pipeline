# v0.12.19 — cluster hygiene + honest audit trail + DLQ rescue (2026-08-09)

## Where this came from

Weekend diagnostics on the "missing" Argus-upgrade triage decision
(follow-up from the SPCX miss review). It wasn't missing — it was
mislabeled, and pulling that thread exposed three distinct defects.

### Fix 1 — C2 symbol-overlap gate (the template blackhole)

Cluster 8693 turned out to contain 40+ UNRELATED tickers (MCHP, DDOG,
ABBV, V, SNAP, KYMR, SPCX…) spanning a week — all matching one Benzinga
headline template, "Firm Maintains Rating on Company, Raises Price
Target to $X". The boilerplate dominates the embedding; the company
barely registers. Consequences: story-level suppression crossed tickers
(the SPCX Argus UPGRADE inherited a KYMR DISCARD), and corroboration
counts were inflated by unrelated companies (likely the old
Boeing/Leidos counting oddity too).

**Fix:** joining a neighbor's cluster now ALSO requires a shared
feed-tagged symbol when both sides have symbols
(`require_symbol_overlap: true` in dedup.yaml). The gate walks the
nearest neighbors (now top-5) and takes the best qualifying one;
symbol-less items (many EDGAR/RSS) are exempt. Same-ticker reprints
still cluster and suppress exactly as before. Bonus: dedup.yaml's
thresholds are now actually read by the service (they were decorative —
constructor defaults happened to match).

Existing contaminated clusters are left in place — with the gate live
they stop growing across tickers, and the 48h dedup window ages the
template neighbors out naturally.

### Fix 2 — SUPPRESS journaled under the item's own ticker (A1)

The Argus suppress row was labeled KYMR (the prior verdict's ticker),
which is why auditing by SPCX found nothing for a day. SUPPRESS rows now
carry the incoming item's own first symbol, falling back to the prior's
only when the item is untagged.

### Fix 3 — data-unavailable symbols journal a REJECT instead of dying in the DLQ (A2)

news.quarantine showed ~10 escalations/day dead-lettering with
`404 Not Found ... /v2/stocks/<SYM>/snapshot` (symbols Alpaca doesn't
carry — OTC, delisted, misparsed) or `no daily bars for <SYM>`. A2 was
retrying a deterministic failure 5× and losing the signal silently. Those
two exact conditions now journal a visible ANALYST/REJECT
("market data unavailable for BCY: 404 from data feed") and stop.
Transient failures (timeouts, DNS, 5xx) still retry → DLQ, which is
correct for conditions that can heal.

## Cost and risk

- Fix 1 changes cluster geometry going forward: more, smaller, cleaner
  clusters; slightly more A1 triage calls (items that used to be
  swallowed by cross-ticker suppression now get a verdict). A1 is the
  resident 8B — cheap.
- Fix 3 converts silent losses into visible REJECT rows — expect a
  handful/day; each names its ticker, so recurring junk symbols become
  diagnosable (and deny-listable upstream later if noisy).

## Files

REPLACED (5): `src/c2_dedup/cluster.py`, `src/c2_dedup/service.py`,
`src/a1_triage/service.py`, `src/a2_analyst/service.py`,
`config/dedup.yaml`.
NEW (3): `tests/unit/test_v01219_fixes.py`, these patch notes, the
deploy guide.
Plus the pencil edit: `pyproject.toml` version → `0.12.19`.

No migration. Three service restarts: `c2-dedup`, `a1-triage`,
`a2-analyst`.

## Tests

Release set **90 green**: 12 new (symbol-overlap gate incl. exemptions
and disable switch, ticker-attribution fallback, 404-vs-transient
classification) + the v0.12.17/18 sets.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.18
sudo systemctl restart c2-dedup a1-triage a2-analyst
```
