# Deploy Guide — v0.12.20 (pre-news exit reference repair)

**What this release does:** fixes the dormant July-24 bug where the
"exit if price gives back the whole news move" protection could never
arm on live positions — the pre-news price was computed by the gate but
never passed into the position's exit policy. One-way data plumbing fix;
no behavior changes for anything else. Full story in
`patch-notes-v0_12_20.md`.

**When to do this: any time this weekend**, after v0.12.19. ~7 minutes.
No migration. Two service restarts.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_20-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   two folders (`src`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/c3_gate/service.py` — with the folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `tests` folders and the two `.md` files.
2. **Two files are REPLACED:** `src/c3_gate/service.py`,
   `src/a3_risk/service.py`. **Three are NEW:**
   `tests/unit/test_prenews_exit_ref.py`, the patch notes, this guide.
3. Commit message: `v0.12.20: pre-news exit reference repair`
4. **Commit changes**, open the commit, confirm **5 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.19"` →
   `version = "0.12.20"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.20` → title
   `v0.12.20 — pre-news exit reference repair` → **Publish**.

## Part 4 — Pull onto the Spark and restart

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.20
sudo systemctl restart c3-gate a3-risk
systemctl is-active c3-gate a3-risk
```

The last line should print `active` twice.

## Part 5 — Verify now

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src EMBEDDER=hash MARKETDATA=fake BROKER=fake .venv/bin/python -m pytest tests/unit/test_prenews_exit_ref.py tests/unit/test_risk_exec.py tests/unit/test_analyst_gate.py tests/unit/test_gate_defer.py tests/unit/test_gate_recheck.py -q'
```

Expect `81 passed`.

**2. The right version is live:**

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.12.20`.

**3. Both services healthy:**

```bash
sudo journalctl -u c3-gate -u a3-risk --since "10 minutes ago" --no-pager | tail -8
```

## Part 6 — The behavioral proof: the NEXT opened position

This fix can only prove itself when a trade actually opens. When the
next position appears (whenever that is), run:

**1. Exit policy carries the reference:**

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT position_id, ticker, exit_policy->>'prenews_price' AS prenews FROM journal.positions ORDER BY opened_ts DESC LIMIT 3;"
```

The newest position's `prenews` column must show a price, not blank.

**2. The arm succeeds** (this is the line that always failed for
position 5):

```bash
psql "$PIPELINE_DSN" -c "SELECT ts, event_type, left(detail,70) FROM journal.position_events WHERE event_type='INVALIDATION_ARMED' ORDER BY ts DESC LIMIT 6;"
```

Every recent row should show a compiled predicate; any `ARM FAILED:
... prenews_price` after this deploy means the fix missed a path — tell
Claude immediately.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.19
sudo systemctl restart c3-gate a3-risk
```
