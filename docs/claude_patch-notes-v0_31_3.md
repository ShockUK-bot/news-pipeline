# Patch notes v0.31.3 (2026-11-10): RSS feed audit, GlobeNewswire feeds removed

## Audit (probed 2026-11-10 10:50 CT with the pipeline's own identity)

| Feed | Status | Last 7 days | Verdict |
|---|---|---|---|
| prnewswire-financial | 200, 20 items | 918 items | keep |
| prnewswire-bustech | 200, 20 items | 1,853 | keep |
| newsfile-global | 200, 25 items | 1,017 | keep |
| nasdaq-halts | 301 to https, then 200 | 772 | keep, URL moved to https |
| fed-monetary | 200, 15 items | 0 (FOMC cadence) | keep |
| bls-latest | 200 with its contact UA | 3 | keep |
| eia-today | 200, 25 items | 2 | keep |
| globenewswire-public and 7 gnw-* lanes | 403 Forbidden to every client | 0 since 2026-10-09 | removed |

GlobeNewswire has refused this client since 2026-10-08, with the honest identifier that worked in August and with plain curl alike; every GlobeNewswire feed lives on the same host, so there is no alternative URL to switch to. The content is not lost: Alpaca's Benzinga stream carries GlobeNewswire releases with their globenewswire.com URLs, 1,914 of them in the week to 2026-11-10 against about 2,900 a week the eight feeds delivered in early October, on tier 2 with full analyst context. In their life the eight feeds produced 2,383 escalations and no position. The catalyst class tags they carried (earnings, M&A, IPO, clinical, insider, class action, management) are inferred by A1 from the headline, and the offering class deterministically by C3 since v0.29.0.

## Changes

- `config/sources.yaml`: the eight GlobeNewswire feed entries replaced by removal notes (history kept in git); `nasdaq-halts` URL to https (the http address has been a permanent redirect). 7 feeds remain; the `ingestion:rss` aggregate row will read "7/7 feeds OK".
- `tests/unit/test_rss_hardening.py`: the GlobeNewswire UA rule now asserts the feeds are absent (re-adding needs a fresh probe); the low volume lane gap test skips removed lanes.
- Dashboard and watchdog reference no feed by name. The eight DEGRADED health rows stop being written once `c1-ingestion` restarts; the watchdog would delete them as orphans after 30 days, so they are deleted now with the operator's approval (statement in the session summary) to clear the dashboard and the digest's "needs you" list immediately.

Version 0.31.3. Suite 949 passed. Service: `c1-ingestion` restart after the close (hard rule: not during the session). Rollback: `git reset --hard v0.31.2` and restart `c1-ingestion`.
