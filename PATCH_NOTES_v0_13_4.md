# v0.13.4 — The globenewswire ReadTimeout: root cause was our own User-Agent (2026-08-15)

Config and tests only. **No production code changes.** One service restart,
which is the first restart since v0.13.3 — this release is meant to be
deployed together with it.

---

## 1. I got the v0.13.3 diagnosis wrong

v0.13.3's notes say the GlobeNewswire feed "is fine — it returned a valid,
current document on every probe" and that the ReadTimeout "was ours, not
theirs" in the sense of a too-short timeout. The first half was an artefact
of where I probed from; the second half was the right conclusion for the
wrong reason.

Probed from the Spark itself, **every** globenewswire.com URL timed out —
including the seven new subject-code lanes that had never been polled from
that host before. Splitting a timeout does nothing for a server that sends
no bytes at all.

## 2. What is actually happening

GlobeNewswire is behind Akamai (`e68143.dsca.akamaiedge.net`), and Akamai
is **tarpitting the browser User-Agent v0.11.1 introduced**. The connection
is accepted, TLS completes, the request goes out — and the server then
sends nothing until the client gives up. Same URL, same machine, same
minute, only the User-Agent differing:

| User-Agent sent | Result |
|---|---|
| `Mozilla/5.0 ... Chrome/124.0.0.0 Safari/537.36` | **0 bytes, timeout** |
| *(no User-Agent header at all)* | **0 bytes, timeout** |
| `curl/8.5.0` | 200, 36,772 bytes, 0.19s |
| `python-httpx/0.28.1` | 200, 36,772 bytes, 0.24s |
| `news-pipeline/0.13.x (+github url)` | 200, 36,772 bytes, 0.16s |
| `Mozilla/5.0 (compatible; news-pipeline/0.13.x; +github url)` | 200, 36,772 bytes |

Note the last row: a string beginning `Mozilla/5.0` passes. **It is browser
impersonation being refused, not bots, and not the word "Mozilla".**

Ruled out on the evidence rather than by assumption: not IPv6 (forcing IPv4
hung identically), not MTU (the same machine pulled 42KB from prnewswire
over the same path with the same browser UA, and 36KB from GlobeNewswire
itself once the UA changed), not `/RssFeed`-specific (the plain homepage
hung too), not per-URL rate limiting (never-polled URLs failed identically).

**This is our own regression, from 20 July.** v0.11.1 added that Chrome
string to get past PR Newswire's 404s. It worked for PR Newswire and it is
what GlobeNewswire started refusing — which is exactly the ReadTimeout
originally reported, sitting unexplained ever since.

## 3. Fix

Per-feed `user_agent` on all eight globenewswire.com feeds:

```yaml
user_agent: "news-pipeline/0.13.4 (+https://github.com/ShockUK-bot/news-pipeline)"
```

No code change — `poll_headers()` has supported per-feed overrides since
v0.12.25, when BLS turned out to demand the opposite of what the PR wires
demand. GlobeNewswire is the third publisher with an incompatible demand,
and the mechanism absorbed it as intended:

- **prnewswire** — 404s honest bots, needs browser impersonation (v0.11.1)
- **bls.gov** — 403s browser impersonation, needs contact info (v0.12.25)
- **globenewswire** — tarpits browser impersonation, accepts anything honest (v0.13.4)

An honest identifying string is chosen over impersonating `curl/8.5.0`: it
tells GlobeNewswire's operations team who we are and gives them a way to
make contact rather than silently escalating. The version inside it is a
**static identifier and does not track releases** — only the exact strings
in the table above are probe-proven, and it should only be edited after
re-probing. It contains no personal data, so a literal in this public repo
is fine (contrast BLS, rule 22).

## 4. Also folded in: the v0.13.3 test breakage

v0.13.3 retired `prnewswire-news`, and `tests/unit/test_macro.py` had a
hand-written list of feed names it looks up in `sources.yaml`, so the
rename produced `KeyError: 'prnewswire-news'` during the deploy. My miss —
I changed the config without checking which tests read it.

That assertion now derives its list instead of hard-coding it, so a future
rename cannot break it for the wrong reason. GlobeNewswire is excluded
there because it now deliberately *does* carry a UA — and that exclusion is
asserted **positively** in `test_rss_hardening.py`, so the two tests can't
both be satisfied by a feed quietly going missing.

Eleven new tests cover `config/sources.yaml` itself, which is the real gap
this exposed — config other tests look up by name is an interface and had
none of its own:

- the dead `prnewswire-news` name and both dead URLs can never return
- PR Newswire category feeds must keep `expect_title_prefix`
- every `subjectcode` lane must keep `require_items`
- subject-code labels stay URL-safe (a `/` or `'` returns HTTP 400)
- **every globenewswire feed must carry a non-browser UA**
- **no per-feed override may contain `Chrome/`, `Safari/`, `AppleWebKit/`
  or `Gecko/`** — an override exists to escape browser impersonation, so
  one that impersonates a browser is the bug it was added to fix
- exactly `bls-latest` plus the globenewswire feeds override the UA, so a
  fourth deviation has to be deliberate
- BLS's UA still comes from the environment, never a literal
- `nasdaq-halts` keeps its adapter, symbol field and tier 1
- `feeds x attempts x connect_timeout` stays under half the market gap
  threshold, so retry settings can never grow far enough for the dead-man
  ladder to fire on a slow cycle

## Changed files

- `config/sources.yaml` (**REPLACED**)
- `tests/unit/test_macro.py` (**REPLACED**)
- `tests/unit/test_rss_hardening.py` (**REPLACED** — 52 tests → 63)

## Tests

63 pass in `test_rss_hardening.py` against this exact `sources.yaml`, and
the corrected `test_macro` assertion was replayed against it directly.

## Still outstanding, not caused by this or v0.13.3

- **`test_cik_map.py::test_end_to_end_stored_with_symbols`** writes to a
  database (it sits in `tests/unit/` but is an integration test), so it
  fails when `PIPELINE_DSN` is unset. The deploy guide deselects it. The
  permanent answer is a `trading_test` database — a separate, one-time job.
- **`test_triage_v047.py::test_confidence_required`** is A1 triage schema
  validation, untouched by either release. The validator now reports
  `tickers: Field required; direction_hint: Field required; ...`, pushing
  `confidence` out of the truncated string the test greps — consistent with
  an earlier release adding required triage fields. The deploy guide has a
  one-minute check against v0.13.2 to confirm that, and it deserves its own
  release once confirmed.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.3
sudo systemctl restart c1-ingestion
```

The eight globenewswire feeds go back to timing out. Nothing else changes —
no schema, no env vars, no permissions.
