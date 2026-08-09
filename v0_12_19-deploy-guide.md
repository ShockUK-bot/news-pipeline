# Deploy Guide — v0.12.19 (cluster hygiene + audit trail + DLQ rescue)

**What this release does:** three defect fixes from the weekend trace.
Story clusters now require a shared ticker (no more "analyst
boilerplate" mega-clusters that let one company's discard suppress
another company's upgrade), suppression rows are journaled under the
right ticker, and escalations for symbols the data feed doesn't carry
get a visible REJECT instead of silently dying after 5 retries. Full
story in `patch-notes-v0_12_19.md`.

**When to do this: any time this weekend** (market closed). ~8 minutes.
No migration. Three service restarts at the end.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_19-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/c2_dedup/cluster.py` — with the folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md` files.
2. **Five files are REPLACED:** `src/c2_dedup/cluster.py`,
   `src/c2_dedup/service.py`, `src/a1_triage/service.py`,
   `src/a2_analyst/service.py`, `config/dedup.yaml`. **Three are NEW:**
   `tests/unit/test_v01219_fixes.py`, the patch notes, this guide.
3. Commit message: `v0.12.19: cluster hygiene + audit trail + DLQ rescue`
4. **Commit changes**, open the commit, confirm **8 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.18"` →
   `version = "0.12.19"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.19` → title
   `v0.12.19 — cluster hygiene + audit trail + DLQ rescue` → **Publish**.

## Part 4 — Pull onto the Spark and restart

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.19
sudo systemctl restart c2-dedup a1-triage a2-analyst
systemctl is-active c2-dedup a1-triage a2-analyst
```

The last line should print `active` three times.

## Part 5 — Verify now

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v01219_fixes.py tests/unit/test_a1_catalyst_rescue.py tests/unit/test_triage_router.py tests/unit/test_scanner.py tests/unit/test_scanner_etf_guard.py -q'
```

Expect `90 passed`.

**2. The right version is live:**

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.12.19`.

**3. C2 came up with the gate on** (look for the startup line and no
errors):

```bash
sudo journalctl -u c2-dedup --since "10 minutes ago" --no-pager | tail -5
```

## Part 6 — The behavioral proof: next market days

**1. Blackhole growth stops.** Cluster 8693 (and 9237) should gain NO
new members from here on:

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT cm.cluster_id, count(*), max(ni.received_ts) AS newest FROM news.cluster_members cm JOIN news.news_items ni ON ni.item_id = cm.item_id WHERE cm.cluster_id IN (8693, 9237) GROUP BY cm.cluster_id;"
```

Run it Monday evening: `newest` should still be a pre-deploy timestamp.

**2. The gate is visibly working** (occasional "neighbor rejected
(symbol disjoint)" lines are the fix doing its job):

```bash
sudo journalctl -u c2-dedup --since "today" --no-pager | grep -c "symbol disjoint"
```

Any number ≥ 0 is fine; a very large number (thousands/day) would mean
the gate is over-firing — tell Claude.

**3. Lost escalations become visible.** Data-unavailable REJECTs now
appear in the journal instead of the quarantine:

```bash
psql "$PIPELINE_DSN" -c "SELECT ts, ticker, left(reason,70) FROM journal.decisions WHERE stage='ANALYST' AND reason LIKE 'market data unavailable%' AND ts > now() - interval '1 day' ORDER BY ts;"
```

And the analyst DLQ inflow should drop to ~zero:

```bash
psql "$PIPELINE_DSN" -c "SELECT count(*) FROM news.quarantine WHERE source='queue:signal.analyst' AND received_ts > now() - interval '1 day';"
```

**4. Suppress rows carry the right ticker** — spot-check that SUPPRESS
tickers now match their item's own symbol:

```bash
psql "$PIPELINE_DSN" -c "SELECT d.ts, d.ticker, ni.symbols, left(ni.headline,45) FROM journal.decisions d JOIN news.news_items ni ON ni.item_id = d.item_id WHERE d.action='SUPPRESS' AND d.ts > now() - interval '6 hours' ORDER BY d.ts DESC LIMIT 10;"
```

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.18
sudo systemctl restart c2-dedup a1-triage a2-analyst
```
