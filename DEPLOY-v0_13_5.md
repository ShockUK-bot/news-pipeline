# Deploy Guide — v0.13.5 (remove dead Business Wire feed, tighten empty-feed checks)

**What this does:** removes the Business Wire feed, which their own server
says has been switched off, and makes sure no other feed can quietly die
the same way without the dashboard telling you.

**Config and test files only. No pipeline code changes.**

**Time:** about 10 minutes. Any time — nothing here is market-sensitive.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_5-pack.zip` from the chat.
2. Right-click → **Extract All** → into a **NEW empty folder**.
3. You'll get `v0_13_5-pack` containing a **config** folder, a **tests**
   folder, and two loose `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **config** folder, the **tests** folder, and both `.md` files.
3. **Two files are REPLACED:**
   - `config/sources.yaml`
   - `tests/unit/test_rss_hardening.py`
4. Commit message: `v0.13.5: remove deactivated businesswire feed, require_items on all feeds`
5. **Commit changes**, then open the commit and confirm **4 changed files**
   (2 replaced + 2 new). Different number → stop, tell Claude.

## Part 3 — Version bump and release

1. `pyproject.toml` → pencil → `version = "0.13.4"` → `version = "0.13.5"`
   → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.13.5` → title
   `v0.13.5 — remove dead Business Wire feed` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.5
```

## Part 5 — Run the tests

```bash
cd /opt/pipeline
```

```bash
export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
```

```bash
env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit -q --deselect tests/unit/test_cik_map.py::test_end_to_end_stored_with_symbols
```

**Expected:** `1 failed, 677 passed` — up 3 from last time, and the single
failure should still be the pre-existing `test_triage_v047.py::test_confidence_required`.

Anything else in the failure list → stop and paste it to Claude.

## Part 6 — Restart ingestion

```bash
sudo systemctl restart c1-ingestion
```

```bash
sudo systemctl status c1-ingestion --no-pager
```

`Active: active (running)` is what you want.

## Part 7 — Clear the orphaned health row

Removing a feed leaves its dashboard row behind, stuck on whatever it last
said, because nothing writes to it any more. Same housekeeping as the
`prnewswire-news` row:

```bash
sudo -u postgres psql -d trading -c "DELETE FROM journal.health WHERE component='ingestion:rss:businesswire-all';"
```

Expect `DELETE 1`.

## Part 8 — Confirm the picture

Wait 2–3 minutes, then:

```bash
sudo -u postgres psql -d trading -c "SELECT component, status, detail FROM journal.health WHERE component LIKE 'ingestion:rss%' ORDER BY component;"
```

**What you want to see:**

- **16 rows**: the `ingestion:rss` aggregate plus 15 named feeds. No
  `businesswire-all`, no `prnewswire-news`.
- The aggregate reading **`OK / 15/15 feeds OK`**.

If the aggregate says something like `14/15 feeds OK`, one feed is failing
right now while its own row still reads OK — that's the transient tolerance
working, not a bug. It only means something if it persists. Find out which:

```bash
sudo journalctl -u c1-ingestion --since '10 min ago' --no-pager -l | grep -iE "poll failed|poll retry|feed stale"
```

`poll retry` and `poll failed (transient)` at WARNING are normal background
noise from the wires. **ERROR** lines mean three consecutive misses and are
worth sending to Claude.

And confirm news is still flowing from the feeds that matter:

```bash
sudo -u postgres psql -d trading -c "SELECT source, count(*) FROM news.news_items WHERE received_ts > now() - interval '30 minutes' GROUP BY source ORDER BY 2 DESC;"
```

---

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.4
```

```bash
sudo systemctl restart c1-ingestion
```

Business Wire returns as a permanently empty feed. Nothing else changes.
