# v0.13.6 — Staleness limits re-derived from measured data (2026-08-16)

Config and tests only. **No production code changes.** One service restart.

---

## 1. The 72h limits were wrong, and they were wrong on their first weekend

`gnw-management` went DEGRADED with *"newest item 3.2d old (limit 72h)"*
within hours of v0.13.5 going live. Probing all seven GlobeNewswire subject
lanes from the Spark on 2026-08-16 showed the limits, not the feeds, were
at fault:

| lane | newest age | window | max gap between items |
|---|---|---|---|
| gnw-earnings | 49.2h | 2.8h | 1.2h |
| gnw-classaction | 0.8h | 7.5h | 1.4h |
| gnw-manda | 51.6h | 11.3h | 5.5h |
| gnw-management | 82.4h | 48.0h | 15.2h |
| gnw-insider | 52.1h | 60.0h | 41.7h |
| gnw-clinical | 0.2h | 109.0h | 59.0h |
| gnw-ipo | 52.1h | **270.9h** | **71.6h** |

Two things I got wrong:

**The binding constraint is market closure, not publishing volume.** Six of
the seven lanes had a newest item **49–52 hours old on a Sunday evening** —
Friday's news, and entirely healthy. A three-day holiday weekend plus one
quiet Friday is roughly **120 hours** of completely legitimate silence. A
limit under that alarms on normal quiet, which trains you to ignore the row.
That is worse than having no check at all.

**These are fixed 20-item windows, so a quiet lane's window spans days.**
`gnw-ipo`'s window was 270.9h wide with a **71.6h gap between consecutive
items** — against a 72h limit. That feed was a coin flip away from a false
alarm from the moment it shipped.

## 2. Rule, not judgement

```
stale_after_hours = max(120, 2 x measured max inter-item gap)
```

Which gives 120h for six lanes, 168h for `gnw-ipo`, and 120h for the PR
wires and Newsfile (up from 48h — the same weekend logic applies to them).
`bls-latest` and `eia-today` keep 168h. `nasdaq-halts` and `fed-monetary`
remain exempt for the reasons already in the config.

Two tests enforce it: **no limit may sit below the 120h floor**, and each
lane must clear twice its measured gap, with the measurements recorded in
the test so a future edit has to argue with data rather than with me.

## 3. What this check is actually for

Worth stating plainly, because I sized it as if it were something else.
`stale_after_hours` answers **"has this source died"** — the WSJ markets
feed that returned valid RSS for 19 months while editorially frozen. It is
**not** a latency alarm. `heartbeat.GapMonitor` and the per-source gap
thresholds own "is ingestion broken", they are untouched by this release,
and none of the dead-man logic is affected. For a death detector, a 5–7 day
window is right.

## 4. Under observation, not fixed: gnw-management

At 82.4h silent it is genuinely ambiguous. Its items arrived every ~15h
through Mon–Thu, then nothing since Thursday — **including all of Friday, a
business day**. That is either a quiet Friday or a feed that stopped, and it
cannot be told apart from here. The new 120h limit will not fire on it.

**Action: look again Monday afternoon.** If it is still silent then, it is
frozen and should be removed:

```bash
sudo -u postgres psql -d trading -c "SELECT max(published_ts) FROM news.news_items WHERE source='rss:gnw-management';"
```

## 5. Deliberately NOT fixed: the prnewswire-bustech 301s

Investigated to a conclusion and closed by choice, not by giving up.

The failure is a **301 redirect to a trailing-slash URL that 404s** — the
slash variant was confirmed 404 directly. Three hypotheses were tested and
all three failed:

| Hypothesis | Test | Result |
|---|---|---|
| Conditional GET breaks it | plain vs `If-None-Match`, 20 requests | plain 4/10, conditional 4/10 — **identical** |
| Rate limiting from bursts | 25 rapid requests, one connection | **all 200** — hammering keeps it healthy |
| Idle triggers it | 90s idle, then request | **200 at t+0.1s** — did not reproduce |

An earlier run did flip to 301 after 60s idle and stayed there, so it is
real and intermittent — most likely edge nodes with inconsistent
canonical-redirect configuration, with a fresh node draw per connection.
`prnewswire-financial` is unaffected as a control.

**And it costs latency, not data.** Each poll returns the last 20 items and
`store_item` dedups by `item_id`, so a failed poll loses nothing. At ~50%
success on a ~100s loop, successful polls land ~200s apart; PR Newswire
would need six business-technology releases per minute for anything to roll
out of a 20-item window unseen.

v0.13.3's retry and 3-strike tolerance already handle it correctly in
production — one retry, WARNING lines, row stays green, feed recovers. The
option of per-feed retry counts with fresh-connection retries was
considered and declined: it would take poll success from ~50% to ~90%+, but
it touches working ingestion code to buy latency on a Tier-3 feed that is
not losing anything.

## Changed files

- `config/sources.yaml` (**REPLACED**) — staleness limits only
- `tests/unit/test_rss_hardening.py` (**REPLACED** — 66 tests → 68)

## Tests

68 pass against this exact `sources.yaml`.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.5
sudo systemctl restart c1-ingestion
```

`gnw-management` goes back to DEGRADED on a limit that was too tight.
