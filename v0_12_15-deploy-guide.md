# Deploy Guide — v0.12.15 (late-pass budget pacing)

**What this release does:** this morning (Wed 2026-08-05) the late pass's
daily budget of 10 was used up by the very first pass at 07:10 ET, and
~140 later pre-market signals (GOOG, LLY, MRVL, SHOP among them) were
dropped with "budget exhausted". This release paces the budget across the
whole pre-market — every 10-minute pass gets its own slice, losers of a
pass go back in the pool to compete again, and the budget mathematically
cannot run out before the last pass before the open. Full story in
`patch-notes-v0_12_15.md`.

**When to do this: any time the market is closed.** ~8 minutes. No
migration. No service restarts. No new timers — nothing to install or
enable this time; the existing a4-late.timer just runs the new code.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_15-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   three folders (`src`, `config`, `tests`) and two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents.** The preview
> must show paths like `src/a4_premarket/late.py` and `config/a4.yaml` —
> with the folders in front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**
   → drag the `src` + `config` + `tests` folders and the two `.md` files.
2. **Three files are REPLACED:** `src/a4_premarket/late.py`,
   `config/a4.yaml`, `tests/unit/test_a4_late.py`. **Two are NEW:** the
   patch notes and this guide.
3. Commit message: `v0.12.15: late-pass budget pacing`
4. **Commit changes**, open the commit, confirm **5 changed files** —
   different number, stop and tell Claude.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → `version = "0.12.14"` →
   `version = "0.12.15"` → **Commit changes**.
2. **Releases → Draft a new release** → tag `v0.12.15` → title
   `v0.12.15 — late-pass budget pacing` → **Publish**.

## Part 4 — Pull onto the Spark

Paste this whole block on the Spark:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.15
```

That's the whole deploy — the late pass is a oneshot script, so the timer
that's already running will use the new code at its next firing. Nothing
to restart.

## Part 5 — Verify

**1. The tests (~5 seconds):**

```bash
cd /opt/pipeline && sudo -u trader bash -c 'PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_a4_late.py tests/unit/test_a4_premarket.py tests/unit/test_triage_router.py tests/unit/test_a5_thematic.py -q'
```

Expect `57 passed`.

**2. The right version is live:**

```bash
sudo -u trader git -C /opt/pipeline describe --tags
```

Expect `v0.12.15`.

**3. Evening safety-guard proof (recommended):** fire the pass manually:

```bash
sudo systemctl start a4-late.service
sudo journalctl -u a4-late.service --since "2 minutes ago" --no-pager | tail -3
```

Expect a `late pass no-op` line — the same-session guard, unchanged:
evening runs refuse to touch the queue.

## Part 6 — The behavioral proof: tomorrow morning

Any time after ~08:00 your time:

**1. Pacing in the logs** — every `late pass done` line now shows
`allowance=`, `passes_left=`, `budget_left=`, and `deferred=`:

```bash
sudo journalctl -u a4-late.service --since today --no-pager | grep "late pass done"
```

What healthy looks like: `allowance=2` on early passes (24 budget / ~15
passes), growing toward `allowance=4` near the open; `budget_left` still
above zero on the LAST pass before 08:30 your time. On a quiet morning
`forwarded=0 deferred=0` all the way is also correct.

**2. Forwards spread across the morning, not bunched at the first pass:**

```bash
export PIPELINE_DSN="$(sudo grep -m1 '^PIPELINE_DSN=' /etc/pipeline/pipeline.env | cut -d= -f2- | tr -d '"')"
psql "$PIPELINE_DSN" -c "SELECT ts, ticker, payload->>'pass_allowance' AS allowance, left(payload->>'headline',50) AS headline FROM journal.decisions WHERE stage='PREMARKET' AND action='LATE_CANDIDATE' AND ts::date=current_date ORDER BY ts;"
```

Expect at most 24 rows, with timestamps spread across 06:10–08:20 your
time on a busy morning (this morning's version put all 10 at 06:10).

**3. Budget-exhausted IGNOREs should be rare now:**

```bash
psql "$PIPELINE_DSN" -c "SELECT count(*) FROM journal.decisions WHERE stage='PREMARKET' AND action='IGNORE' AND payload->>'late'='true' AND ts::date=current_date;"
```

This morning: ~140. Tomorrow: expect 0 unless the morning is truly
extreme (only possible once all 24 slots are spent).

**4. No watchdog email** — nothing restarts in this deploy.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.14
```

(No timers or services to touch — the next firing simply runs the old
code again.)
