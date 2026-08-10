# Deploy Guide — v0.12.22 (turn on bounded discretion)

**What this release does:** flips one config word that has silently kept
A3's risk-sizing model turned off since the feature shipped (every trade
used profile defaults), and cleans up the confusing error text that got
journaled each time. Your SNDK 1-share size was correct and is not
changed by this — see `patch-notes-v0_12_22.md`.

**When to do this: after the close today, or any evening.** ~6 minutes.
No migration. One service restart.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_22-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `config/risk.yaml` — with the folders in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md`
   files.
2. **Two files are REPLACED:** `config/risk.yaml`,
   `src/a3_risk/service.py`. **Three are NEW:**
   `tests/unit/test_a3_discretion_live.py`, the patch notes, this
   guide.
3. Commit message: `v0.12.22: enable A3 bounded discretion (was stub)`
4. **Commit changes**, open the commit, confirm **5 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.21"` →
   `version = "0.12.22"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.22` → title
   `v0.12.22 — enable A3 bounded discretion` → **Publish**.

## Part 4 — Pull onto the Spark and restart

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.22
sudo systemctl restart a3-risk
systemctl is-active a3-risk
```

The last line should print `active`.

## Part 5 — Verify now

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src EMBEDDER=hash MARKETDATA=fake BROKER=fake .venv/bin/python -m pytest tests/unit/test_a3_discretion_live.py tests/unit/test_risk_exec.py tests/unit/test_prenews_exit_ref.py -q'
```

Expect `38 passed`.

**2. The right version is live:**

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.12.22`.

## Part 6 — The behavioral proof: the next sized trade

When the next trade gets sized (whenever that is), the RISK decision
should show the model actually participating:

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -x -c "SELECT ts, ticker, payload->>'model_used' AS model_used, payload->'adjustments' AS adjustments FROM journal.decisions WHERE stage='RISK' AND agent='A3' AND action='SIZE' ORDER BY ts DESC LIMIT 1;"
```

- `model_used` should be `true`, and `adjustments.reason` should be the
  model's own sentence (not "fallback…" and not "profile defaults").
- If it still says `fallback to profile defaults: …`, the model call is
  failing — the trade is still sized safely, but tell Claude and paste
  the line; likely the :8081 slot wasn't up.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.21
sudo systemctl restart a3-risk
```
