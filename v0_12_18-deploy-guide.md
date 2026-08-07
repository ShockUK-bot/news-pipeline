# Deploy Guide — v0.12.18 (triage catalyst rescue + dedup cooldown)

**What this release does:** the news lane's half of the SPCX miss. The
triage model now treats lock-up expirations and real analyst upgrades/
downgrades as catalysts even when they arrive inside a "shares are
trading higher" story, and a discarded story no longer blackholes its
follow-ups for 24 hours (2 hours now — enough to kill wire reprints,
short enough that a new catalyst gets a fresh look). Full story in
`patch-notes-v0_12_18.md`.

**Deploy v0.12.17 FIRST** (if you haven't already), then this one — the
two releases are the two halves of the same fix and the version bumps
assume that order.

**When to do this: any time the market is closed.** ~8 minutes. No
migration. One service restart at the end.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_18-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/a1_triage/prompt.py` — with the folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md` files.
2. **Four files are REPLACED:** `src/a1_triage/prompt.py`,
   `src/a1_triage/suppression.py`, `src/a1_triage/service.py`,
   `config/a1.yaml`. **Three are NEW:**
   `tests/unit/test_a1_catalyst_rescue.py`, the patch notes, this guide.
3. Commit message: `v0.12.18: triage catalyst rescue + dedup cooldown`
4. **Commit changes**, open the commit, confirm **7 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.17"` →
   `version = "0.12.18"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.18` → title
   `v0.12.18 — triage catalyst rescue + dedup cooldown` → **Publish**.

## Part 4 — Pull onto the Spark and restart triage

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.18
sudo systemctl restart a1-triage
systemctl is-active a1-triage
```

The last line should print `active`.

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_a1_catalyst_rescue.py tests/unit/test_triage_router.py tests/unit/test_scanner.py tests/unit/test_scanner_etf_guard.py -q'
```

Expect `78 passed`.

**2. The right version is live:**

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.12.18`.

**3. Triage is processing:** after a few minutes,

```bash
sudo journalctl -u a1-triage --since "10 minutes ago" --no-pager | tail -5
```

## Part 6 — The behavioral proof: the next few market days

**1. The escalation share stays sane** — the prompt change should add a
handful of escalations per day, not re-open the floodgates. Run this in
the evening for a few days:

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT action, count(*), round(100.0*count(*)/sum(count(*)) OVER (),1) AS pct FROM journal.decisions WHERE stage='TRIAGE' AND ts::date=current_date GROUP BY action ORDER BY action;"
```

The July baseline was roughly 10–25% ESCALATE. If ESCALATE climbs past
~40%, tell Claude — that's the v0.4.7 incident shape and the prompt
needs a counterweight.

**2. Rating changes now escalate** — on any day with a real upgrade or
downgrade of a liquid name, it should appear as TRIAGE/ESCALATE, and its
reason should name the rating-change class:

```bash
psql "$PIPELINE_DSN" -c "SELECT ts, ticker, left(reason,70) FROM journal.decisions WHERE stage='TRIAGE' AND action='ESCALATE' AND ts::date=current_date AND reason ILIKE '%rating%' ORDER BY ts;"
```

**3. Suppressions into discards are now short-fused** — SUPPRESS rows
referencing a DISCARD should only cite the 2h window:

```bash
psql "$PIPELINE_DSN" -c "SELECT ts, ticker, left(reason,80) FROM journal.decisions WHERE stage='TRIAGE' AND action='SUPPRESS' AND ts::date=current_date ORDER BY ts DESC LIMIT 10;"
```

Rows saying `(..., DISCARD) within 2h` are the new behavior; `within
24h` should now appear only with ESCALATE priors.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.17
sudo systemctl restart a1-triage
```
