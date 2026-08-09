# Deploy Guide — v0.12.21 (stale briefing recommendations)

**What this release does:** stops the morning email recommending action
on positions that are already closed (your JNJ report). The briefing now
checks last night's review recommendations against the positions that
are actually open this morning and silently drops the ones that no
longer apply — recording how many it dropped so the fact sheet stays
honest. Full story in `patch-notes-v0_12_21.md`.

**When to do this: any time before Monday pre-open.** ~6 minutes. No
migration. **No service restarts** — the briefing is a scheduled
morning job that loads fresh code every run.

**Deploy v0.12.20 first** if you haven't yet — version bumps assume the
order.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_21-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   two folders (`src`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/a8_briefing/facts.py` — with the folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `tests` folders and the two `.md` files.
2. **One file is REPLACED:** `src/a8_briefing/facts.py`. **Three are
   NEW:** `tests/unit/test_a8_stale_recos.py`, the patch notes, this
   guide.
3. Commit message: `v0.12.21: drop stale recos from morning briefing`
4. **Commit changes**, open the commit, confirm **4 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.20"` →
   `version = "0.12.21"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.21` → title
   `v0.12.21 — stale briefing recommendations` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.21
sudo -u trader git -C /opt/pipeline describe --tags
```

The last line should print `v0.12.21`. That's it — no restarts.

## Part 5 — Verify now

**The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src EMBEDDER=hash MARKETDATA=fake BROKER=fake .venv/bin/python -m pytest tests/unit/test_a8_stale_recos.py tests/unit/test_a8_briefing.py tests/unit/test_prenews_exit_ref.py tests/unit/test_risk_exec.py tests/unit/test_analyst_gate.py tests/unit/test_gate_defer.py tests/unit/test_gate_recheck.py -q'
```

Expect `89 passed`.

## Part 6 — The behavioral proof: Monday's email

1. **No JNJ.** The morning email must not mention JNJ or recommend
   closing anything (book is flat).
2. **Subject line** shows `0 position recos`.
3. The journaled fact sheet shows the stale reco was consciously
   dropped, not lost:

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT payload->'facts'->'a6'->'review'->>'stale_recos_dropped' AS dropped FROM journal.decisions WHERE stage='SYSTEM' AND agent='A8' AND action='BRIEFING' ORDER BY ts DESC LIMIT 1;"
```

Expect `1` (the JNJ reco). When a new position opens later and A6
writes a fresh review, this returns to blank/0 naturally.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.20
```

(no restarts either way)
