# v0.13.5 — Business Wire is dead; empty-feed assertions applied consistently (2026-08-16)

Config and tests only. **No production code changes.** One service restart.

---

## 1. Business Wire: removed, not replaced

`businesswire-all` has been answering **HTTP 200 with a valid, parseable,
completely empty feed**. Its own `<description>` says why:

> *"The RSS channel you requested was deactivated by the administrator."*

The `?rss=G1QFDERJXkJeEFpRWQ==` token is a saved-channel id and Business
Wire has switched it off. Probed from the Spark on 2026-08-15/16:

- `feed.businesswire.com` returns the **same 951 bytes to every User-Agent
  tried** — honest, `curl/8.5.0`, browser, and even `NewsNow` (one of the
  two agents their robots.txt explicitly permits). So this is not a
  User-Agent problem and not the robots.txt restriction; the channel is
  simply off.
- `www.businesswire.com`, which is where a replacement URL would have to be
  discovered, returns **403 to a browser UA and read-times-out on an honest
  one**. Both doors shut. No replacement is findable from your machine.

So it is removed rather than repointed. Guessing at another saved-channel
token would be exactly the mistake v0.11.2 made and then withdrew.

**Coverage impact: small.** The wire lane is now GlobeNewswire (8 feeds),
PR Newswire (2), Newsfile (1), plus Alpaca's Benzinga stream on tier 2. In
the 30 minutes after the v0.13.4 restart those feeds delivered 243 items;
Business Wire delivered zero, and had been delivering zero for an unknown
length of time.

## 2. The part that matters more than the feed

**It was green on the dashboard the entire time.** `ingestion:rss:businesswire-all`
read `OK / polled` while the source was dead, because v0.13.3 shipped
`require_items` on nine feeds and left it off this one — on the reasoning
that its robots.txt risk made it a "works until it doesn't" dependency.

That was backwards. The feed I had already identified as fragile is the one
that most needed the assertion. Had it carried `require_items`, the row
would have gone yellow and we would know *when* it died instead of only
that it had.

So the selection is no longer a judgement call:

- **Every feed now carries `require_items`.** The single exception is
  `nasdaq-halts`, where an empty response genuinely means "nothing is
  halted" — the good outcome, not a fault.
- **`stale_after_hours` is applied wherever a publishing cadence exists**
  to measure against: 48h on the wires, 72h on the GlobeNewswire subject
  lanes, 168h on `bls-latest` and `eia-today`. Exempt: `nasdaq-halts`
  (quiet is good) and `fed-monetary` (FOMC statements are weeks apart by
  design — a limit there would alarm on normal silence rather than a
  fault).

Three new tests make this impossible to get wrong again: the dead
businesswire name and host can never return; the set of feeds without
`require_items` must equal exactly `{"nasdaq-halts"}`; and the set without
`stale_after_hours` must equal exactly `{"nasdaq-halts", "fed-monetary"}`.
These assert on **set equality, not membership**, so adding a feed without
thinking about either key fails the suite rather than passing quietly.

## 3. Deliberately NOT fixed: the prnewswire-bustech 404s

Two `poll failed (transient)` warnings appeared for `prnewswire-bustech`
just after the v0.13.4 restart, with a **trailing slash the client never
sent** appended to the URL before the 404 — the exact signature recorded in
the v0.11.2 notes.

Investigated and closed with no action, on evidence:

| Test | Result |
|---|---|
| Plain GET, no redirect following | 200, no `Location` header |
| Conditional GET (`If-None-Match` + `If-Modified-Since`) | **304, no redirect** |
| Five rapid repeats | `[200, 200, 200, 200, 200]` |

The conditional-GET hypothesis — that the CDN mishandles `If-None-Match`,
which would neatly explain "first poll works, all later polls 404" — is
**disproven**. Both PR Newswire feeds handle conditional requests correctly.
It is transient bot-mitigation, fifteen hours stale by the time it was
tested.

**And v0.13.3 already handled it correctly in production**: one retry, two
WARNING lines, health row left green, feed recovered by itself. That is
precisely why 404 sits in `RETRYABLE_STATUS` and why `feed_degrade_after`
is 3. Changing anything here would be fixing a system that worked.

## 4. Confirmed in production: the v0.13.4 User-Agent fix

Worth recording, because it is the cleanest before/after this project has
produced. From one `journalctl` window spanning the restart:

```
21:52:28  ERROR  poll failed  feed=globenewswire-public  error=ReadTimeout('')
21:54:02  ERROR  poll failed  feed=globenewswire-public  error=ReadTimeout('')
   ... every ~90s, without a single success ...
22:03:24  ERROR  poll failed  feed=globenewswire-public  error=ReadTimeout('')
--- restart 22:04:07, v0.13.3+v0.13.4 code and config ---
22:05:33  (nothing further from globenewswire-public)
```

Same machine, same feed, same cadence. The only change was the string in
the `User-Agent` header. All eight GlobeNewswire feeds now poll normally
and stored 160 items in the first half hour.

## Changed files

- `config/sources.yaml` (**REPLACED**) — 16 feeds → 15
- `tests/unit/test_rss_hardening.py` (**REPLACED** — 63 tests → 66)

`tests/unit/test_macro.py` is unchanged: its assertion derives its feed list
(v0.13.4), so removing a feed does not break it. That was the point.

## Tests

66 pass against this exact `sources.yaml`. Worst-case poll cycle is now
15 feeds x 2 attempts x 10s = **300s**, against an 1800s market gap
threshold — pinned by a test.

## Housekeeping done live during diagnosis

The orphaned `ingestion:rss:prnewswire-news` health row (left behind by
v0.13.3's rename, and stuck reading `OK` because nothing writes to it any
more) was deleted by hand. `ingestion:rss:businesswire-all` will be
orphaned the same way by this release — clear it after restarting:

```bash
sudo -u postgres psql -d trading -c "DELETE FROM journal.health WHERE component='ingestion:rss:businesswire-all';"
```

Note the database is **`trading`**, not `pipeline` — my earlier deploy
guides had that wrong.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.4
sudo systemctl restart c1-ingestion
```

Business Wire comes back as a permanently empty feed. Nothing else changes.
