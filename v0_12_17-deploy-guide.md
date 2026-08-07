# Deploy Guide — v0.12.17 (scanner universe + news-ownership fix)

**What this release does:** SPCX's +7% unlock-day move was invisible to
the scanner for two reasons — its candidate list only ever contained
tiny stocks making giant moves, and a bug made it stand down whenever
the news pipeline had *seen* a ticker, even if it had thrown every story
away. After this release, the scanner also watches the 50 most-traded
names in the market every minute, and it only stands aside when the news
lane genuinely escalated something. Full story in
`patch-notes-v0_12_17.md`.

**When to do this: any time the market is closed.** ~8 minutes. No
migration. One service restart at the end.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_17-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/c10_scanner/service.py` — with the folders
> in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md` files.
2. **Four files are REPLACED:** `src/c10_scanner/service.py`,
   `src/c10_scanner/screener.py`, `config/scanner.yaml`,
   `tests/unit/test_scanner.py`. **Two are NEW:** the patch notes and
   this guide.
3. Commit message: `v0.12.17: scanner universe + news-ownership fix`
4. **Commit changes**, open the commit, confirm **6 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.16"` →
   `version = "0.12.17"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.17` → title
   `v0.12.17 — scanner universe + news-ownership fix` → **Publish**.

## Part 4 — Pull onto the Spark and restart the scanner

Paste this whole block:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.17
sudo systemctl restart c10-scanner
systemctl is-active c10-scanner
```

The last line should print `active`. (One watchdog-quiet restart of a
single service — no other services are touched.)

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_scanner.py tests/unit/test_scanner_etf_guard.py -q'
```

Expect `47 passed`.

**2. The right version is live:**

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.12.17`.

**3. The scanner came back healthy:**

```bash
sudo journalctl -u c10-scanner --since "5 minutes ago" --no-pager | tail -5
```

Outside 08:50–14:15 your time it idles quietly — that's correct.

## Part 6 — The behavioral proof: next market day

Any time after ~09:30 your time:

**1. The universe widened** — candidates with single-digit/teens moves
now appear in the journal (before, nothing under +26% ever did):

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT ts, ticker, status, reject_reason, round((metrics->>'move_pct')::numeric*100,1) AS move_pct FROM journal.scanner_candidates WHERE scan_date=(now() AT TIME ZONE 'America/New_York')::date AND (metrics->>'move_pct')::numeric < 0.25 ORDER BY ts LIMIT 20;"
```

Rows here — even FILTERED ones — are the new leg working: liquid names
in the 4–25% band being examined at all is the fix. Empty on a day when
no liquid name moved 4%+ is also correct.

**2. NEWS_OWNS_IT is rarer and honest** — any SUPPRESSED_NEWS row must
now have a real TRIAGE/ESCALATE within 4 hours before it:

```bash
psql "$PIPELINE_DSN" -c "SELECT ts, ticker FROM journal.scanner_candidates WHERE status='SUPPRESSED_NEWS' AND scan_date=(now() AT TIME ZONE 'America/New_York')::date;"
```

For any ticker it prints, this must return at least 1:

```bash
psql "$PIPELINE_DSN" -c "SELECT count(*) FROM journal.decisions WHERE ticker='PUT-TICKER-HERE' AND stage='TRIAGE' AND action='ESCALATE' AND ts > now() - interval '8 hours';"
```

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.16
sudo systemctl restart c10-scanner
```
