# v0.12.17 — scanner universe + news-ownership fix (2026-08-07)

## The miss this fixes

SPCX (SpaceX) rose ~7% on Thursday 2026-08-06 — its 911M-share lock-up
expiry day — and the system never looked at it. The full trace
(`claude/no-trade-review-2026-08-07.md`) found two independent scanner
defects; both are fixed here. (The news lane's part — triage discarding
catalyst-bearing reaction stories and dedup inheriting a discarded
cluster's verdict — is deliberately NOT touched in this release; it is a
model/prompt change queued for the weekend review with a week of
evidence.)

### Defect 1 — the scan universe could not contain SPCX

The scanner's only feed was the top-20 movers list, which is ranked by
percent change. On any normal day those 20 slots are all >25% microcap
spikes (the two-day journal shows every examined candidate moved +26% to
+325%). A liquid mega-cap at +7% mathematically never enters, which made
`min_move_pct: 4%` decorative — the feed cut off six times higher than
the filter. The scanner was structurally blind to big liquid names making
clean single-digit moves: the easiest trades in the book.

**Fix:** the universe is now top-gainers UNION top-50 most-actives by
volume (`merge_universe`, pure and unit-tested). The most-actives client
already existed — it was never wired into the scan loop. One batch
snapshot call per scan fills price/percent-change for the actives so
quiet high-volume names (NVDA drifting +0.4%) are skipped by the existing
cheap pre-filter without any per-ticker measurement; a name only pays the
full measurement once it clears the 4% floor. Same filters, same caps,
same scoring judge both feeds. Config: `scanner.most_actives_top: 50`
(0 disables the new leg).

### Defect 2 — NEWS_OWNS_IT counted suppressed-into-discarded stories

The news cross-check stood the scanner down when ANY non-DISCARD triage
row existed for the ticker in the last 4 hours — and SUPPRESS rows are
non-DISCARD. A suppressed duplicate of a *discarded* story therefore told
the scanner "the news lane is handling this" when the news lane had
thrown it away. Both SPCX (Wednesday afternoon) and AEVA (Thursday
08:50, journaled SUPPRESSED_NEWS/NEWS_OWNS_IT) hit this exact trap.

**Fix:** only `action = 'ESCALATE'` counts as ownership. A
discard/suppress history is exactly when the scanner backstop must stay
live. The weak-match path (headlines attached to the emission as A2
context) is unchanged, so A2 still sees what the news said.

## What would have happened with this release live

SPCX enters via most-actives (it was the most active name in the market
on unlock day), passes every filter (price ~$105, ADV in the billions,
+7%, deep book), no ESCALATE exists to suppress it, and it emits to A2
with the unlock headlines attached as weak-match context. A2 and C3
remain the judges — this release only guarantees they get the chance.

## What it deliberately does NOT change

- Filters, thresholds, caps (max 2/scan, 6/day, breaker): untouched.
- A1 triage prompt and C2 dedup: untouched (weekend follow-up).
- Emission mechanics, Tier-1 synthetic items, the C3 scanner gate:
  untouched.

## Files

REPLACED (4): `src/c10_scanner/service.py`, `src/c10_scanner/screener.py`,
`config/scanner.yaml`, `tests/unit/test_scanner.py`.
NEW (2): these patch notes, the deploy guide.
Plus a one-line pencil edit: `pyproject.toml` version → `0.12.17`.

No migration. One service restart: `c10-scanner`.

## Tests

Scanner release set **47 green** (test_scanner incl. the new universe
merge, SPCX-shaped filter pass, and the ESCALATE-only pin;
test_scanner_etf_guard unchanged).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.16
sudo systemctl restart c10-scanner
```
