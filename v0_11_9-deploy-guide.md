# Deploy Guide — v0.11.9 (heavy model: read `reasoning_content`)

**What this does:** makes A4/A5/A7 actually use the heavy off-hours model
instead of falling back to their no-LLM path. Fixes the "primary model
unavailable / deterministic fallback mode" briefing line.

**One code file changes. No systemd change, no database change, no model
restart.** This is the lowest-risk kind of deploy: nothing needs to be
restarted at market close, and nothing touches the broker.

---

## Part 1 — Get the pack

Download `v0_11_9-pack.zip` and extract it. You'll get a `src` folder, a `tests`
folder, and two `.md` files.

## Part 2 — Upload to GitHub

1. Go to `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **src** folder, the **tests** folder, and the two `.md` files.
3. **One file is REPLACED:** `src/a1_triage/backends.py`.
   One file is NEW: `tests/unit/test_backend_reasoning.py`.
4. Commit message:
   `v0.11.9: read reasoning_content fallback for heavy slot`
5. Commit. Confirm **4 changed files** (1 replaced + 1 new test + 2 new `.md`).

## Part 3 — Version bump + release

1. Open `pyproject.toml`, change `version = "0.11.8"` → `version = "0.11.9"`,
   commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.9` → title
   `v0.11.9 — heavy model reasoning_content fix` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.9
```

That's the whole deploy. **Nothing needs restarting right now:**

- The heavy-slot agents — **A4** (pre-market, 07:00 ET), **A5** (thematic,
  21:30 ET), **A7** (reports) — run on timers. Each one starts a brand-new
  process on its next scheduled run, so it reads the new code automatically.
  You do **not** need to restart anything for the fix to take effect.
- The always-on agents (triage, analyst) use the smaller production models,
  which were never affected by this bug — no action needed. (If you *want*
  everything on identical code, you can restart them at any quiet moment with
  `sudo systemctl restart a1-triage a2-analyst` — optional, and safe, but not
  required.)

## Part 5 — Confirm it worked (this evening / tomorrow morning)

There's no immediate dashboard change to look for — the proof comes on the next
heavy-slot run.

**Tonight after 21:30 ET (A5 thematic):**

```bash
sudo journalctl -u a5-thematic -n 40 --no-pager | grep -iE "slot|model|fallback|thematic"
```

You want to see it using the model (`slot=heavy` / a `model_id`) and producing a
thematic update — **not** a "fallback" / deterministic line.

**Tomorrow after 07:00 ET (A4 pre-market):**

```bash
sudo journalctl -u a4-premarket -n 40 --no-pager | grep -iE "slot|model|sheet|fallback"
```

Same idea: a real ranked sheet from the model, not the deterministic fallback.

**The clearest signal** is the **2026-07-22 morning briefing**: it should no
longer contain "The system is operating in deterministic fallback mode with the
primary model unavailable." If that line is gone, the fix is confirmed end to
end.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.8
```

(If you restarted `a1-triage`/`a2-analyst` in Part 4, restart them once more
after the rollback. Nothing else to undo — no systemd or database changes.)
