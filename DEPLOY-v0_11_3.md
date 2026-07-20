# Deploy Guide — v0.11.3 (A1 triage: force full-field emission)

**What this release does:** fixes the reason tickers stopped showing up on
the C6 decision tape. It doesn't touch the model, the model server, or any
of the RSS/EDGAR work from earlier this week — it's a one-file change to
how strictly A1 (the triage step) requires the model to answer every field.
No database changes, no new services, no new permissions.

**When to do this:** any time — it restarts A1 triage only (`a1-triage`),
nothing else. ~5 minutes. This is unrelated to v0.11.1/v0.11.2 (the RSS
feed work) — you can deploy this whether or not you ever deploy v0.11.2.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_3-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_11_3-pack` containing one `src` folder and the two loose `.md`
   files.

## Part 2 — Upload to GitHub (browser, same as before)

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload
   files**.
2. Drag in the **src** folder and the two `.md` files from
   `v0_11_3-pack`.
3. **One file is REPLACED this time** (GitHub handles it automatically):
   `src/a1_triage/schema.py`.
4. Commit message: `v0.11.3: A1 triage — require full field emission`
5. **Commit changes**, then open the commit and confirm **3 changed
   files** (1 replaced + 2 new `.md` files). Anything different → stop,
   tell Claude.

## Part 3 — Version bump + release

1. Open `pyproject.toml` in the repo → pencil (edit) icon → change
   `version = "0.11.1"` to `version = "0.11.3"` (we're skipping 0.11.2 on
   purpose — that was the PR Newswire URL patch that turned out not to be
   needed) → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.3` → title
   `v0.11.3 — A1 triage full-field fix` → **Publish**.

## Part 3.5 — Optional: run the test suite first (recommended, but skip if you'd rather just deploy)

If you want a bit more confidence before this goes live, run this on the
Spark **after** Part 4's `git checkout` but **before** the restart:

```bash
cd /opt/pipeline
sudo -u trader /opt/pipeline/.venv/bin/python -m pytest -q
```

Look for a line like `XX passed` with no `failed`. If you see failures,
copy the last 20-30 lines to Claude before restarting the service — don't
restart on a failing test run. If this feels like too much, it's fine to
skip; the change was verified locally against the live model in this
session already.

## Part 4 — Pull onto the Spark and restart A1 triage

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.3
sudo systemctl restart a1-triage
sudo systemctl status a1-triage --no-pager
```

Look for `Active: active (running)`. If it says `failed` or keeps
restarting, stop and copy the last 20 lines of
`sudo journalctl -u a1-triage -n 20 --no-pager` to Claude.

Note: this does **not** need `c1-ingestion` or `llama-a1` restarted — only
`a1-triage` picks up the new schema.

## Part 5 — Confirm it worked

Give it 15-30 minutes to accumulate some ESCALATE decisions (longer if the
market's quiet), then run:

```bash
export PIPELINE_DSN=postgresql://trader:<your-password>@127.0.0.1:5432/trading
psql "$PIPELINE_DSN" -c "
SELECT date_trunc('hour', ts) AS hr, action, count(*) AS total,
       count(ticker) AS with_ticker
FROM journal.decisions
WHERE stage='TRIAGE' AND ts > now() - interval '3 hours'
GROUP BY 1,2 ORDER BY 1,2;"
```

- **ESCALATE rows with `with_ticker` close to `total`** → it worked, tickers
  should start reappearing on the C6 tape.
- **A new `REJECT` row appears with meaningful count** → not a failure of
  this patch — it means A1's retry-then-give-up path is now correctly
  catching cases the model still can't fully answer even with the error
  hint. Expected to be occasional, not the majority. If REJECT count is
  large relative to ESCALATE, tell Claude — that means the prompt itself
  needs a follow-up tweak for this model, separate from this fix.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.1
sudo systemctl restart a1-triage
```

Nothing else to undo.
