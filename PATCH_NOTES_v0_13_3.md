# v0.13.3 — RSS: stop the false alarms, replace the dead feed, add the ticker-bearing ones (2026-08-15)

Triggered by two reported errors — `globenewswire` ReadTimeout and
`prnewswire` 404 — which turned out to be two different problems, one of
them ours. While investigating I probed every wire feed we use and a long
list we don't; the full survey is in `claude/news-source-review-2026-08-15.md`.
This release is the code and config that came out of it.

**No trading logic, gate, sizing, exit or execution behaviour changes.**
C1 ingestion only.

---

## 1. The globenewswire ReadTimeout was ours, not theirs

The feed is fine — it returned a valid, current document on every probe on
2026-08-15. `rss.py` used a **single flat 20-second timeout covering
connect and read together, with no retry**. A big wire feed that connects
instantly but takes 22 seconds to send its body raised `ReadTimeout`, and
because the per-feed row went DEGRADED on the *first* miss, one slow
response was enough to log an ERROR and turn that feed's dashboard light
yellow.

Three fixes, which compound:

- **Split timeouts.** Connect 10s / read 25s by default
  (`rss.connect_timeout_secs`, `rss.read_timeout_secs`), overridable per
  feed. `globenewswire-public` gets `read_timeout_secs: 40`.
- **Retry inside one poll.** `rss.retries` (default 1) extra attempts with
  a short backoff, for transport errors and for the statuses that are
  usually transient or bot mitigation rather than truth
  (403/404/408/425/429/5xx). Content-assertion failures are deliberately
  *not* retried — a wrong-title feed will still be wrong 1.5s later.
- **Per-feed failure tolerance.** `rss.feed_degrade_after` (default 3)
  consecutive failed polls before the row goes DEGRADED. Earlier failures
  log WARNING and leave the row alone. This is the same shape as v0.11.7
  (EDGAR) and v0.11.11 (the RSS aggregate); the per-feed row was the last
  place in C1 that still went red on a single miss.

Worst case for a total network outage is
`feeds x (retries+1) x connect_timeout` ≈ 6 minutes of cycle time (16 feeds),
well inside the 1800s market gap threshold, so `heartbeat.GapMonitor` and
the dead-man ladder are unaffected.

**Known consequence, stated plainly:** the poller is still serial, and the
feed count went from 6 to 16. A healthy cycle now takes roughly 30–40s of
fetching on top of the 60s `poll_interval_secs`, so the effective revisit
rate is closer to ~100s than 60s. That is fine for wire feeds and slightly
worse than ideal for `nasdaq-halts`, which is the one latency-sensitive
feed in the list. If halt latency turns out to matter, the fix is per-feed
poll intervals or a concurrent cycle with a small semaphore — both are real
changes to the loop's shape and deliberately not bundled into a release
whose job is to stop false alarms.

## 2. The prnewswire 404 is real and permanent this time

`https://www.prnewswire.com/rss/news-releases-list.rss` → **404**,
confirmed 2026-08-15 from two independent directions. The alternate URL
prepared in v0.11.2 (`all-news-releases-from-PR-newswire-news.rss`) →
**also 404**. Unlike July 2026, this did not recover: PR Newswire has
retired the all-news feed. Their **category** feeds do work.

Replaced with the two categories relevant to equities:

| Feed name | Category |
|---|---|
| `prnewswire-financial` | Financial Services & Investing |
| `prnewswire-bustech` | Business Technology |

**Only two, and this is the important part.** Of the 14 PR Newswire
categories that resolve, only four were actually fresh on 2026-08-15 —
`energy` was 26 days stale, `general-business` 64 days, `consumer-products-retail`
69 days. The staleness is real and not a sort-order artefact: for `health`,
the feed's own `lastBuildDate` matched its newest item exactly. The two
above plus `telecommunications` and `sports` were the fresh ones; the first
two are the ones worth having.

## 3. A 200 OK does not mean you got the feed you asked for

Probing turned up three silent failure modes, none of which a status-code
check can see. All three are now asserted, opt-in per feed, and all three
have a test that reproduces the real-world case:

- **PR Newswire serves a fallback feed for categories that don't exist.**
  A deliberately made-up slug returned valid RSS with 20 fresh items. The
  only tell is an **empty channel `<title>`**. Without an assertion we
  would ingest the wrong feed indefinitely and never see an error.
  → `expect_title_prefix`.
- **GlobeNewswire returns a valid, EMPTY feed for an unknown subject code**,
  echoing back whatever `feedTitle` you asked for. A typo in a code
  silently yields zero news forever. → `require_items`.
- **A feed can be structurally healthy and editorially dead.** The WSJ
  markets feed (`feeds.a.dj.com/rss/RSSMarketsMain.xml`) returns perfectly
  valid RSS whose newest item is from **January 2025** — 19 months frozen,
  HTTP 200 throughout. → `stale_after_hours`.

Title and item assertions are treated as **fetch-equivalent failures**: we
did not get the feed we asked for, so nothing is stored and the
conditional-GET cache is not advanced.

Staleness is deliberately **not** a fetch failure. The fetch worked, so
`mark_activity()` still fires and the aggregate row stays green; only that
one feed's own row goes yellow with a `stale:` detail. A publisher going
quiet must never look like ingestion dying — that distinction is the whole
point of the gap thresholds.

## 4. New feeds

All free, all verified live on 2026-08-15, all inside the existing poller —
no new component, no new credentials, no new environment variables.

**Nasdaq trading halts** (`nasdaq-halts`, tier 1) — the highest-value free
feed found in the survey. It carries the **ticker as a machine-readable
field**, covers halts on all US venues rather than just Nasdaq listings,
and a T1 (news pending) halt *is* the event, minutes before any wire
carries the release.

It needs a small adapter, for a concrete reason: **the items have no
`<guid>` and no `<link>`**, so `normalize_rss` would quarantine every
single one (verified — it raises `MISSING_REQUIRED_FIELD`), and the
`<title>` is the bare ticker rather than a headline. Everything real lives
in an `ndaq:` namespace that feedparser flattens to `ndaq_issuesymbol`,
`ndaq_haltdate` and so on (verified against the live document). The
adapter synthesises a stable, halt-specific guid and a readable headline.

**On the halt timestamp — a deliberate choice worth knowing about.**
`<ndaq:HaltTime>` carries **no timezone**, and probing found unrelated
symbols sharing an identical millisecond value, which is a batch artefact
rather than a per-symbol halt instant. Guessing a zone risks being 4–5
hours wrong in a pipeline where event time drives intraday behaviour. So:
an item first seen **on the day it halted** is stamped with the receive
time (accurate to the 60s poll interval); anything from an **earlier ET
date** is stamped at that date's ET midnight, which means the startup
backlog can never masquerade as fresh. The raw `HaltDate`/`HaltTime` are
preserved in `raw` for whoever wants to interpret them later.

**Newsfile** (`newsfile-global`) — dense US small/mid-cap coverage, the
size band the majors under-serve. 25 items per poll, verified fresh.

**Seven GlobeNewswire subject lanes** — same host we already poll, split by
catalyst type so A1 sees the event class without inferring it: earnings
(13), M&A (27), IPOs (21), clinical study (90), insider buy/sell (22),
class action (84), management changes (86).

Note on those URLs: the **numeric code alone selects the content**. The
text after the hyphen and the whole `feedTitle` segment are cosmetic, so
they are set to `x` — a label containing `/` or `'` (e.g. subject 22,
"Insider's Buy/Sell") returns **HTTP 400** unless double-encoded. Not worth
the risk for a cosmetic string.

## 5. Structured symbols, not inference

`normalize_rss` gains an optional `symbols` argument, and feeds gain
`symbol_fields`. This reads a **dedicated machine-readable field the
publisher provides** (`<ndaq:IssueSymbol>`), which is a feed tag in the
sense of the normalize.py module docstring. Nothing reads a headline or
body looking for tickers — that remains A1's job. A feed with no
`symbol_fields` and no `adapter` behaves byte-for-byte as it did before.

## 6. One risk you should know about, unchanged by this release

`feed.businesswire.com/robots.txt` **disallows `/rss/` for everyone except
two named agents** (`NewsNow` and `Tiingo News`). We are neither. The feed
answers us today and `businesswire-all` is untouched here, but treat it as
a works-until-it-doesn't dependency rather than a stable one. It now
carries `stale_after_hours: 48` so a quiet death is visible.

---

## Changed files

- `config/sources.yaml` (**REPLACED**) — new feeds + transport settings
- `src/c1_ingestion/sources/rss.py` (**REPLACED**)
- `src/c1_ingestion/normalize.py` (**REPLACED**) — one optional argument
  and one line in `normalize_rss`; every other function untouched
- `tests/unit/test_rss_hardening.py` (**NEW** — 52 tests)

## Tests

`tests/unit/test_rss_hardening.py`, 52 tests, no database and no network:
the tolerance ladder, split/overridden timeouts, retry classification
(including 404-is-retryable and content-failure-is-not), all three content
assertions reproduced from the real feeds, staleness, symbol handling, and
the halts adapter end-to-end through `normalize_rss`.

Verified before shipping:
- 52 new tests pass; the existing `tests/unit/test_health_recovery.py`
  (which covers `aggregate_health`, unchanged here) still passes alongside.
- The `ndaq:` field names were confirmed by parsing the live document with
  feedparser rather than assumed.
- The whole `RssSource` loop was driven against a mock transport covering
  a flaky feed (ReadTimeout on attempt 1 → retried → never goes yellow), a
  hard 404 (silent for two cycles, DEGRADED on the third), a fallback feed
  (assertion fires, **nothing stored**), and the halts feed (tier 1,
  `symbols=['TALK']`, readable headline).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.2
sudo systemctl restart c1-ingestion
```

Nothing else to undo — no schema change, no new env vars, no new sudoers,
no new timers.
