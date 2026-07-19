# Deploy Guide — v0.10.0 (Earnings calendar)

**What this release does:** until now the system had no idea when companies
report earnings — every trade was allowed with an "earnings unknown" note
in the journal, so a short-term position could accidentally be held through
its own earnings announcement (the single most violent overnight gap risk
there is). This release fetches the upcoming earnings calendar for the
whole US market once a day and switches on the blackout rule that has been
sitting ready in the risk code since Phase 4: no new short-lane entry
within one session of that company's report date.

**When to do this:** tonight, right after v0.9.0 — or any evening this
week. ~15 minutes, and you'll create one free API key (needs only an
email address). **Deploy v0.9.0 FIRST** — this release builds on it.

---

## Part 1 — Get your free Alpha Vantage key (2 minutes, once)

1. In your browser go to: `https://www.alphavantage.co/support/#api-key`
2. Fill in the short form (say "Investor", any organization name, your
   email) → **GET FREE API KEY**.
3. The key appears on the page (a short code like `A1B2C3D4E5F6G7H8`).
   Copy it into a notepad — you'll paste it in Part 6.

The free key allows 25 requests per day; the system makes exactly ONE per
day. No credit card, no cost.

## Part 2 — Get the pack onto your PC

1. Download `v0_10_0-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `v0_10_0-pack` containing `src`, `config`, `ops`, `schema`, `tests`
   and the two loose `.md` files.

## Part 3 — Upload to GitHub (browser, same as before)

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **src**, **config**, **ops**, **schema**, **tests** and the two
   `.md` files.
3. **Two files are REPLACED this time** (GitHub handles it automatically):
   - `src/a3_risk/service.py` — the earnings lookup goes live
   - `src/a2_analyst/context.py` — the analyst now sees earnings dates
4. Commit message: `v0.10.0: earnings-calendar source`
5. **Commit changes**, then open the commit and confirm **11 changed
   files** (9 new + 2 changed), including
   `src/c1_ingestion/earnings.py` and
   `schema/migrations/005-earnings-calendar.sql`. Anything missing →
   stop, tell Claude.

## Part 4 — Version bump + release

1. `pyproject.toml` → pencil → `version = "0.9.0"` → `version = "0.10.0"`
   → commit to `main`.
2. **Releases → Draft a new release** → tag `v0.10.0` → title
   `v0.10.0 — earnings calendar` → **Publish**.

## Part 5 — Pull onto the Spark + database migration

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.10.0
sudo -u postgres psql -d trading_test -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/005-earnings-calendar.sql
sudo -u postgres psql -d trading -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/005-earnings-calendar.sql
```

Both migration commands should end with `COMMIT` and no `ERROR` lines.

## Part 6 — Add the API key to the environment file

```bash
sudo nano /etc/pipeline/pipeline.env
```

Arrow down to the bottom and add ONE new line, pasting your key from
Part 1 after the `=` (no spaces, no quotes):

```
ALPHAVANTAGE_KEY=PASTE_YOUR_KEY_HERE
```

Save and exit: **Ctrl-O**, **Enter**, **Ctrl-X**.

## Part 7 — Run the test suite

```bash
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/ -q'
```

Expect **357 passed, 0 failed** (9 more than v0.9.0). Any failure → stop,
copy the last 30 lines to Claude.

## Part 8 — Install the timer and run the first fetch NOW

```bash
sudo cp /opt/pipeline/ops/systemd/earnings-calendar.service /opt/pipeline/ops/systemd/earnings-calendar.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now earnings-calendar.timer
sudo systemctl start earnings-calendar.service
journalctl -u earnings-calendar.service --no-pager -n 5
```

The log should end with `earnings refresh done` and a `rows_upserted`
count in the thousands. Then confirm the data is really there:

```bash
sudo -u postgres psql -d trading -c "SELECT count(*), min(report_date), max(report_date) FROM news.earnings_calendar"
```

You should see a count of several thousand and dates spanning the next
~3 months. If the log says `refresh failed` mentioning the key → re-check
Part 6 (the key line), then `sudo systemctl start earnings-calendar.service`
again.

`systemctl list-timers earnings-calendar.timer --no-pager` should show
NEXT = tomorrow 04:15 your time (05:15 ET, before the pre-market review).

## Part 9 — What changes in practice

- New short-lane entries within one session of that company's earnings
  report are now **vetoed** (`EARNINGS_BLACKOUT` in the decision tape and
  the EOD email's veto list) instead of allowed-with-a-flag.
- The analyst (A2) sees `earnings_date` for every ticker it evaluates,
  and its context now also lists matching standing theses from the
  Phase-8 store.
- The `EARNINGS_UNKNOWN` flag should largely disappear from the journal —
  if it persists on well-known tickers, the fetch is failing (check
  `journalctl -u earnings-calendar.service` and the dashboard's health
  panel, component `earnings`).
- If the provider ever breaks, nothing stops working — the system just
  reverts to flagging EARNINGS_UNKNOWN until the next successful fetch.

## Rollback (if anything misbehaves)

```bash
sudo systemctl disable --now earnings-calendar.timer
sudo -u trader git -C /opt/pipeline checkout v0.9.0
```

Leave migration 005 in place (additive and harmless).
