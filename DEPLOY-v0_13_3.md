# Deploy Guide — v0.13.3 (RSS fix + new feeds)

**What this release does, in plain terms:** it stops your RSS line going
yellow over nothing, replaces the PR Newswire feed that has genuinely died,
and adds nine new free news feeds — including Nasdaq's trading-halt feed,
which tells you a stock has been halted before any newswire carries the
story.

**When to do this:** any time, but **a weekend or after the close is
better**. Reason: the Nasdaq halt feed arrives with a backlog of recent
halts. Deploying when the market is shut means every one of those backlog
items is from a previous day, so the code stamps them with their own date
and nothing can look like breaking news that isn't. Deploying mid-session
is safe, it's just noisier.

**Time:** about 20 minutes, most of it waiting.
**Builds on:** v0.13.2. Deploy that first if you haven't.

**Nothing here touches the database, your trading settings, gates, sizing,
or execution.** No new passwords, no new keys, no new permissions.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_13_3-pack.zip` from the chat.
2. Right-click it → **Extract All** → extract into a **NEW empty folder**
   (making a new folder each time is what stops old files sneaking into a
   release).
3. You'll get a folder called `v0_13_3-pack` containing:
   - a **config** folder
   - a **src** folder
   - a **tests** folder
   - two loose files: `PATCH_NOTES_v0_13_3.md` and `DEPLOY-v0_13_3.md`

## Part 2 — Upload to GitHub (in your browser, same as always)

1. Go to `github.com/ShockUK-bot/news-pipeline`.
2. Click **Add file → Upload files**.
3. Drag in, from inside the extracted `v0_13_3-pack` folder: the **config**
   folder, the **src** folder, the **tests** folder, and both loose `.md`
   files. (GitHub keeps the folder structure, so files land in the right
   places and replacements happen automatically.)
4. **Three files are REPLACED this time:**
   - `config/sources.yaml`
   - `src/c1_ingestion/sources/rss.py`
   - `src/c1_ingestion/normalize.py`
5. Commit message:
   `v0.13.3: RSS retry/timeout/assertions, replace dead prnewswire feed, add halts + 8 feeds`
6. Click **Commit changes**.
7. Open the commit you just made and check it says **6 changed files**
   (3 replaced + 3 new). If it says anything else, **stop and tell Claude
   the number** — don't carry on.

## Part 3 — Version bump and release

1. In the repo, click `pyproject.toml` → the pencil (edit) icon.
2. Change `version = "0.13.2"` to `version = "0.13.3"`.
3. Commit to `main`.
4. On the right of the repo page: **Releases → Draft a new release**.
5. Tag: `v0.13.3` · Title: `v0.13.3 — RSS hardening + new feeds` →
   **Publish release**.

## Part 4 — Pull it onto the Spark

Open a terminal on the Spark and run these **one line at a time**:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
```

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.3
```

You should see it mention switching to `v0.13.3`. If it complains that
**"local changes would be overwritten"**, STOP and paste the message to
Claude — don't force it.

## Part 5 — Run the tests BEFORE restarting anything

```bash
cd /opt/pipeline
```

```bash
export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test
```

```bash
.venv/bin/python -m pytest tests/unit -q
```

The last line should say something like **N passed** — and N should be
**52 higher** than it was on v0.13.2, because that's how many new tests
this release adds.

If you see the word **FAILED** anywhere, stop and paste the output to
Claude. Don't restart the service.

## Part 6 — Check the new feeds actually answer from YOUR machine

This is worth 60 seconds. Some of these feeds behave differently depending
on who's asking, and it's better to find that out now than from a yellow
dashboard tomorrow. Copy this whole block and paste it in one go:

```bash
cd /opt/pipeline && .venv/bin/python - <<'PY'
import httpx, feedparser
urls = {
 "prnewswire-financial": "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
 "prnewswire-bustech":   "https://www.prnewswire.com/rss/business-technology-latest-news/business-technology-latest-news-list.rss",
 "globenewswire-public": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
 "newsfile-global":      "https://feeds.newsfilecorp.com/global/Last25Stories",
 "gnw-earnings":         "https://www.globenewswire.com/RssFeed/subjectcode/13-x/feedTitle/x",
 "gnw-manda":            "https://www.globenewswire.com/RssFeed/subjectcode/27-x/feedTitle/x",
 "gnw-ipo":              "https://www.globenewswire.com/RssFeed/subjectcode/21-x/feedTitle/x",
 "gnw-clinical":         "https://www.globenewswire.com/RssFeed/subjectcode/90-x/feedTitle/x",
 "gnw-insider":          "https://www.globenewswire.com/RssFeed/subjectcode/22-x/feedTitle/x",
 "gnw-classaction":      "https://www.globenewswire.com/RssFeed/subjectcode/84-x/feedTitle/x",
 "gnw-management":       "https://www.globenewswire.com/RssFeed/subjectcode/86-x/feedTitle/x",
 "nasdaq-halts":         "http://www.nasdaqtrader.com/rss.aspx?feed=tradehalts",
}
ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
for name, url in urls.items():
    try:
        r = httpx.get(url, headers={"User-Agent": ua}, follow_redirects=True,
                      timeout=httpx.Timeout(connect=10, read=40, write=40, pool=10))
        p = feedparser.parse(r.text)
        title = (p.feed.get("title") or "").strip() or "(EMPTY TITLE)"
        print(f"{name:24} HTTP {r.status_code}  items={len(p.entries):3}  title={title[:44]}")
    except Exception as e:
        print(f"{name:24} ERROR {type(e).__name__}: {e}")
PY
```

**What you want to see:** every line saying `HTTP 200`, with `items=` a
number bigger than 0.

Two things to look at specifically:

- The two **prnewswire** lines must show a real title starting
  `All Financial Services` / `All Business Technology`. If either says
  **`(EMPTY TITLE)`**, that category has been retired too — tell Claude,
  don't deploy around it.
- **nasdaq-halts** showing `items=0` is **completely fine** — it means
  nothing is halted right now, which is the good outcome.

If one or two feeds error, that's survivable (the new code tolerates it,
that's the whole point), but paste the output to Claude anyway so we know
what's flaky before it shows up as a yellow light.

## Part 7 — Restart ingestion

```bash
sudo systemctl restart c1-ingestion
```

```bash
sudo systemctl status c1-ingestion --no-pager
```

Look for `Active: active (running)` near the top. If it says `failed` or
keeps restarting, stop and send Claude the output of:

```bash
sudo journalctl -u c1-ingestion -n 30 --no-pager
```

## Part 8 — Confirm it's working (give it 2–3 minutes first)

```bash
sudo journalctl -u c1-ingestion --since '3 min ago' --no-pager -l | grep -i "poll stored\|poll failed\|feed stale\|poll retry"
```

**What good looks like:**

- Several `poll stored` lines with feed names from the new list — the
  system is taking in news.
- `poll retry` lines are **fine and expected occasionally**. That's the new
  code catching a slow response and quietly having another go instead of
  crying about it. That is the fix working, not a problem.
- `poll failed (transient)` at WARNING level is also fine — same thing.
- `poll failed` at **ERROR** level means that feed has now missed **three
  polls in a row**. That one is worth telling Claude about.

Then check the dashboard health panel. You should now see a row per feed:

```
ingestion:rss                  (aggregate — should be green)
ingestion:rss:prnewswire-financial
ingestion:rss:prnewswire-bustech
ingestion:rss:globenewswire-public
ingestion:rss:businesswire-all
ingestion:rss:newsfile-global
ingestion:rss:gnw-earnings
ingestion:rss:gnw-manda
ingestion:rss:gnw-ipo
ingestion:rss:gnw-clinical
ingestion:rss:gnw-insider
ingestion:rss:gnw-classaction
ingestion:rss:gnw-management
ingestion:rss:nasdaq-halts
ingestion:rss:fed-monetary
ingestion:rss:bls-latest
ingestion:rss:eia-today
```

The old `ingestion:rss:prnewswire-news` row will still be sitting there
from before, stale and yellow, because nothing writes to it any more.
**That's cosmetic and harmless.** If it bothers you, clear it with:

```bash
sudo -u postgres psql -d pipeline -c "DELETE FROM journal.health WHERE component='ingestion:rss:prnewswire-news';"
```

(If that command errors on the database name, don't worry about it — leave
the stale row alone and mention it to Claude.)

## Part 9 — Check the halt feed did something sensible

Only useful if there were halts recently. Have a look:

```bash
sudo -u postgres psql -d pipeline -c "SELECT published_ts, symbols, headline FROM news.news_items WHERE source='rss:nasdaq-halts' ORDER BY received_ts DESC LIMIT 10;"
```

**What you want to see:** headlines like
`Trading halt: TALK — Talkspace, Inc. Common Stock [T12]`, with the
`symbols` column showing the actual ticker (e.g. `{TALK}`) rather than
being empty. That ticker is what lets a halt route to the intraday path
instead of being treated as general news.

If the `symbols` column is empty on every row, tell Claude — it means
Nasdaq changed their field names and the adapter needs updating.

---

## If you need to undo it

```bash
sudo -u trader git -C /opt/pipeline checkout v0.13.2
```

```bash
sudo systemctl restart c1-ingestion
```

That's the whole rollback. Nothing else to undo — no database changes, no
new settings files, no new permissions. The new feeds simply stop being
polled and the old (broken) prnewswire URL comes back.
