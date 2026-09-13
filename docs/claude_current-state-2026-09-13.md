# Current state, 2026-09-13 (Sunday, market closed)

## Version live

- Tag `v0.14.5` plus documentation commits on top (no code or config changes since the tag).
- All long running agents and components active; scheduled units all reported success on their last run.
- Working tree matches the v0.14.5 code and config (`config/risk.yaml`, `config/a2.yaml`, `config/a12.yaml`, `config/scanner.yaml` unchanged from the tag).

## What was done today

### Claude Code setup (commit `edce282`)
- Added `CLAUDE.md` briefing and `.claude/settings.json` permissions so Claude Code can operate on the box.

### Service list fix (commit `7b84668`)
- The Services section of `CLAUDE.md` listed units that do not exist (`a7-heavy`, `llama-server`, `llama-analyst`) and had `llama-heavy` on the wrong port.
- Rewritten to match `ops/systemd/` and `systemctl list-unit-files` exactly, split into long running units, timer driven oneshots, support timers, and the four inference slots (`llama-a1` :8080, `llama-a2` :8081, `llama-a2b` :8082 shadow, `llama-heavy` :8084 manual only).

### Docs folder and stale unit (commit `8148f5b`)
- Created `docs/` with a README describing the handoff convention.
- Deleted `ops/systemd/llama-analyst.service` (obsolete, never installed).

### A3 risk heartbeat investigation (this commit)
Question: why was the `journal.health` `risk` heartbeat stale, was a3-risk restarted for v0.14.5, and is `disable_thinking` in `risk.yaml` active?

Findings on the box:
- `risk` heartbeat now OK and refreshing every 60 seconds. c7-watchdog raised no `risk` alert since Aug 28 (only a `marketdata` blip on 09-11 11:32, recovered 12:07).
- a3-risk was restarted cleanly on 08-31 (v0.14.4), 09-03 16:56 (v0.14.5, config version `e1845ef`) and 09-11 16:45. No crashes, `NRestarts=0`.
- `disable_thinking: true` is in `risk.yaml` and is wired: A3 builds its model client via `get_backend(cfg["model"])` in `src/a1_triage/backends.py`, which sends `chat_template_kwargs {"enable_thinking": false}` per request. Every news lane sizing call since 09-04 has `model_used: true` on `qwen3.6-27b-q5_k_m` with zero fallback warnings. Scanner lane rows show `model_used: false` by design (no discretion on that lane).
- `tests/unit/test_v0_14_5.py`: 16 passed.

Cause, supplied from the design side and consistent with the evidence:
- The v0.14.4 changeset (periodic heartbeats for risk, dedup, chat; `common/health.py`; watchdog freshness checks) was tagged but never written to the working tree, so those services wrote a heartbeat only at startup. On the morning of 09-11 the `risk` row was about 11,400 minutes old.
- A re-sync on 09-11 restored the files from the v0.14.5 tag and restarted a1, a2, a3, a13, c2, c4 at 16:45 CT. The pre re-sync files are in git stash `stash@{0}` ("pre-v0.14.4 working tree (skipped release) 2026-09-11"). Do not drop it without asking.
- The "known open item" line in `CLAUDE.md` is replaced by a resolved note with this cause, plus a gotcha that `journal.health` has no history (one row per component) and the c7-watchdog journal is the place to reconstruct stale/recovered timelines.

### Tidy ups (this commit)
- `CLAUDE.md` shell recipe now sources `/etc/pipeline/pipeline.env` directly; there is no `.env` symlink.
- `patch-notes-v0_14_5.md` moved to `docs/claude_patch-notes-v0_14_5.md` (renamed to the handoff convention). `v0_14_5-deploy-guide.md` moved to `docs/`.

No services were restarted today. No migrations. No config changes.

## Open items

1. `ingestion:alpaca` heartbeat last updated 2026-09-09 18:52 ("connected, wildcard subscribed"). The watchdog is not alarming on it, so it is probably a startup only row like the old risk one, but worth confirming whether that component should have a periodic heartbeat and a `max_age_min` in `config/watchdog.yaml`.
2. Process lesson from the v0.14.4 miss: after tagging a release, run `git diff <tag> --stat` and expect empty output before restarting services. Consider adding this to the deploy guide template.
3. GitHub push status for today's commits: see the end of the session summary from Claude Code (push attempted after this file was written).

## Next steps

- Nothing scheduled for Monday 09-14 beyond normal operation. First market day after the 09-11 re-sync was 09-11 itself (a3 restarted after close), so Monday is the first full session on the fully synced v0.14.5 tree; check the 09-14 A3 decision mix and the `risk`, `dedup`, `chat` heartbeats stay fresh through the day.
