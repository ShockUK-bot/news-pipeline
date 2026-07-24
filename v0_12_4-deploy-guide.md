# Deploy Guide — v0.12.4 (heavy-model narratives fixed + chat wake fix)

**What this release does:** fixes the "deterministic fallback / narrative
unavailable" problem in your morning briefings, EOD reports, pre-market
sheets, and nightly reviews. Your probes proved the big off-hours model was
spending its entire answer budget "thinking" and never producing the JSON —
one request-level switch turns thinking off, and the same model answered
perfectly (you watched it happen: variant B). Also fixes the chat tab's
off-hours model wake, which pointed at a service name that doesn't exist on
your machine.

**When to do this: any time.** ~10 minutes. No trading service restarts —
the report agents are one-shot jobs that pick the fix up automatically on
their next scheduled run.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_12_4-pack.zip` from the chat.
2. Right-click → **Extract All** → into a NEW empty folder. You'll get
   `src`, `config`, and `tests` folders plus two loose `.md` files.

## Part 2 — Upload to GitHub

> ⚠️ **Drag the FOLDERS themselves, not their contents** — the upload
> preview must show paths like `src/a1_triage/backends.py` with folders in
> front.

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files** →
   drag the three folders + two `.md` files.
2. **Eight files are REPLACED:**
   - `src/a1_triage/backends.py`, `src/a7_report/service.py`
   - `config/a4.yaml`, `config/a5.yaml`, `config/a6.yaml`,
     `config/a7.yaml`, `config/a8.yaml`, `config/a13.yaml`

   **Three files are NEW:**
   - `tests/unit/test_backend_nothink.py`
   - `patch-notes-v0_12_4.md`, `v0_12_4-deploy-guide.md`
3. Commit message: `v0.12.4: disable heavy-model thinking (narratives fixed) + a13 wake fix`
4. Commit, open the commit, confirm **11 changed files** with folder paths.

## Part 3 — Version bump + release

1. `pyproject.toml` → pencil icon → version to `"0.12.4"` → **Commit**.
2. **Releases → Draft a new release** → tag `v0.12.4` → title
   `v0.12.4 — heavy narratives fixed` → **Publish**.

## Part 4 — Pull onto the Spark + one restart

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.4
sudo systemctl restart a13-chat
```

That restart is only for the chat wake fix. A4/A5/A6/A7/A8 are one-shot
timer jobs — every run starts fresh from the deployed code, nothing to
restart. The live trading services (triage/analyst/gate/risk/exec/guard)
are completely untouched by this release.

## Part 5 — Verify

**Optional, right now (~5 min):** re-run your variant-B probe if you like —
but you already saw it pass; the release just wires that switch into every
heavy call.

**The real proof arrives on schedule:**

- **Tonight 21:30 ET** — A5's thematic pass:
  `sudo journalctl -u a5-thematic --since "20:00" --no-pager | grep -i "invalid\|fallback"` → should print nothing.
- **Tomorrow ~07:35 ET** — the morning briefing email should open with a
  real written summary of the session ahead — no "deterministic fallback
  mode" sentence.
- **Tomorrow 15:35 CT** — the EOD report should open with a real narrative
  paragraph — no "(narrative unavailable — model offline…)".
- **Chat, off-hours** — ask the CHAT tab something when the market's
  closed; it should wake the model instead of erroring.

If any of those still shows fallback language, paste that agent's
journalctl window — but given the probes, they won't.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.3
sudo systemctl restart a13-chat
```
