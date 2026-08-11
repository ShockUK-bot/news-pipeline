# Deploy Guide — v0.12.26 (the thesis store starts trading)

**What this release does:** your five standing theses stop being purely
advisory. Every night at 9:15 PM your time, a new code-only job reads the
store and plans small, wide-stopped, long-horizon positions in the quiet
beneficiaries of your strongest "up" theses — executed next morning 15
minutes after the open, through the exact same order machinery as every
trade so far. It would NOT have bought RIOT after its +26% pop (it skips
extended names by design) — it exists so you already OWN the next RIOT
before its morning.

Guardrails, because this is the money path: 0.25% risk per position (half
a news trade), max 2 new per day, max 4 thesis positions total, wide
3×ATR stops, and when a thesis dies in the store the position is exited
automatically. Everything is journaled and emailed.

**Risk: medium** (it plans real paper trades) but the execution path is
untouched — C4 runs its normal preflight, and one command turns the whole
lane off. **Time: ~15 minutes. When: any time — nothing needs restarting**
(entries only ever fire at the next market open + 15 minutes).

---

## Part 1 — Get the pack

Download `v0_12_26-pack.zip` → right-click → **Extract All** into a NEW
empty folder.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in **src**, **config**, **ops**, **schema**, **tests** and the two
   `.md` files.
3. **One file is REPLACED:** `config/exit_profiles.yaml`. Ten are NEW
   (including `src/c11_thesis/service.py` and
   `schema/migrations/012-thesis-origin.sql`).
4. Commit message: `v0.12.26: C11 thesis-entry lane`
5. Confirm **11 changed files** on the commit.

## Part 3 — Version bump + release

`pyproject.toml` → pencil icon → `version = "0.12.26"` → commit →
**Releases → Draft a new release** → tag `v0.12.26` → **Publish**.

## Part 4 — Pull + migrate (on the Spark)

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.26
sudo -u postgres psql -d trading_test -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/012-thesis-origin.sql
sudo -u postgres psql -d trading -v ON_ERROR_STOP=1 -f /opt/pipeline/schema/migrations/012-thesis-origin.sql
```

Both migrations should end with `COMMIT`, no `ERROR` lines.

## Part 5 — Test

```bash
sudo rm -rf /tmp/qdrant-*
cd /opt/pipeline
sudo -u trader bash -c 'export PYTHONPATH=src EMBEDDER=hash QDRANT_PATH=/tmp/qdrant-test MARKETDATA=fake BROKER=fake \
  PIPELINE_DSN=postgresql://trader:PASSWORD@127.0.0.1:5432/trading_test && .venv/bin/python -m pytest tests/unit/test_thesis_entry.py tests/integration/test_thesis_entry_flow.py -q'
```

Expect **17 passed, 0 failed**. (Full suite: same 8 known date-drift
failures as before, 17 more passed.)

## Part 6 — Install the timer

```bash
sudo cp /opt/pipeline/ops/systemd/thesis-entry.service /opt/pipeline/ops/systemd/thesis-entry.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now thesis-entry.timer
systemctl list-timers 'thesis-*' --no-pager
```

NEXT should show **21:15 your time** (22:15 ET) tonight.

## Part 7 — See the first plan (optional, right now)

The timer fires tonight either way, but you can run the planner by hand —
it's idempotent per day, so tonight's firing will quietly no-op after:

```bash
sudo systemctl start thesis-entry.service
sudo journalctl -u thesis-entry -n 10 --no-pager
```

The log line ends with `planned=N`. Then look at what it decided:

```bash
sudo -u postgres psql -d trading -c "SELECT ticker, action, reason FROM journal.decisions WHERE agent='C11' ORDER BY ts DESC LIMIT 12;"
sudo -u postgres psql -d trading -c "SELECT intent_id, ticker, qty, limit_price, status FROM journal.intents WHERE intent_id LIKE 'thesis-%';"
```

You'll also get a **"Thesis entries"** email listing every planned entry
with its thesis id, plus every skip and why (`extended` = too hot right
now, `illiquid`, `min_risk` = the stock is too expensive for a $250 risk
budget — the SNDK situation).

**What to expect from the first plan:** up to 2 entries drawn from your
three "up" theses (AI Infrastructure 0.60, Defense Autonomy 0.55,
Small-Cap Activist 0.50). Anything that already popped — RIOT included —
shows up as an `EXTENDED` skip. That's the discipline working, not a miss.

## Part 8 — Tomorrow morning

Entries execute at **8:45 your time** (09:45 ET, 15 min after open),
5 minutes apart, as day-limit orders at yesterday's close +2%. If a name
gaps past that, the order never fills and expires — chase protection at
the execution layer. Check after 9:00 CT:

```bash
sudo -u postgres psql -d trading -c "SELECT ticker, qty_open, avg_entry, origin, profile FROM journal.positions WHERE status='OPEN' ORDER BY opened_ts;"
```

New rows with `origin=thesis, profile=thesis_v1` = the store is trading.
From then on: A12 guards them on news, A6 reviews them nightly, the
dashboard shows them tagged `thesis`, and the nightly C11 pass exits any
whose thesis dies and flags earnings trims per your policy.

## The off switch

```bash
sudo systemctl disable --now thesis-entry.timer
```

That stops all future planning instantly. Existing positions keep their
stops and exit normally. (Full rollback commands are in the patch notes.)
