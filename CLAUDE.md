# CLAUDE.md — news-pipeline (multi-agent AI trading system on DGX Spark)

You are Claude Code running ON the production box (`spark-d126`, aarch64, DGX OS / Ubuntu 24.04) as the `trader` user, inside `/opt/pipeline`. This system trades a real Alpaca **paper** account every US market day. Treat it like production: measured changes, verify before and after, never guess at schema or service names.

The operator is Ian. He is not a Linux or git user. He designs releases with Claude in a separate chat and brings you the scope or patch notes to implement. Explain what you did in plain language; do not assume he can read a stack trace.

## What the system is

A locally hosted, news and sentiment driven, multi-agent trading pipeline for US equities. 13 agents (A1 triage through A13 operator chat) plus supporting components C1 through C10, built in versioned phases. Currently at tag `v0.14.5` (verify with `git describe --tags`).

Core principles, locked in and not up for revision:
- Pipeline, not conversation: strict JSON contracts between stages.
- Models propose, code disposes. Deterministic gates for all numeric checks.
- Everything journaled with config version tracking. Postgres is the single source of truth.
- Two tier stops: broker resident catastrophe stop plus synthetic layers. Risk derived position sizing.
- Path A catalyst triggered entries; paper trading only. `AlpacaBroker` hard codes `paper-api`. Never change that.
- X/Twitter is rejected as a data source. Do not add it.
- ETB only shorting (no hard to borrow). Validated as money saving; keep it.

## Where things are

- Repo: `/opt/pipeline` (this directory). Owned by `trader`. Remote: GitHub `ShockUK-bot/news-pipeline` (private). Git identity `pipeline@local / "Pipeline Build"`.
- Code: `src/`. Config: `config/*.yaml` (a1.yaml, a2.yaml, risk.yaml, scanner.yaml, watchdog.yaml, ...). Migrations: `schema/migrations/NNN-name.sql`. systemd units: `ops/systemd/*.service` (installed copies in `/etc/systemd/system/`). Tests: `tests/unit/`.
- Python venv: `/opt/pipeline/.venv`. Run tests with `env -u PIPELINE_DSN .venv/bin/python -m pytest tests/unit -q`.
- Secrets: `/etc/pipeline/pipeline.env` and `mailer.env`. You are denied reading them. Confirm presence of a key with `sudo -n grep -c KEYNAME /etc/pipeline/pipeline.env`; never print values. Interactive shell env: `export PYTHONPATH=src && set -a && source .env && set +a` (`.env` is a symlink to pipeline.env; sourcing is fine, catting is not).
- Models: `/opt/models`. llama.cpp: `/opt/llama.cpp`, out of tree builds in dated dirs (`build-2026-08/`); never overwrite the running binary, build beside it.
- Runtime folders that are always untracked and must be left alone: `ops/soak-logs/`, `src/news_pipeline.egg-info/`, `var/`.
- Docs the operator uploads to his chat project live in `docs/` (see Handoff).

## Services (systemd)

Agents: `a1-triage a2-analyst a3-risk a4-premarket a4-late a5-thematic a6-nightly a6-eod a7-eod a7-heavy a8-briefing a12-guard a13-chat`.
Components: `c1-ingestion c2-dedup c3-gate c4-exec c5-mailer c6-dashboard c7-watchdog(.timer) c8-regime c10-scanner`.
Inference: `llama-server` (triage, :8080), `llama-analyst` (:8081, Qwen3.6-27B-UD-Q5_K_M with MTP speculative decoding), `llama-heavy` (:8082, shadow/upgrade staging).
Health: `curl -s http://127.0.0.1:8081/health`. Heartbeats: `journal.health`.

Control with `sudo -n systemctl ...` and logs with `sudo -n journalctl -u <unit> -n 200 --no-pager`. Sudo is scoped to systemctl, journalctl, and /etc/pipeline read helpers. If a command needs more than that, stop and tell Ian exactly what to run himself.

## Database

PostgreSQL 16, database `trading`, DSN in `$PIPELINE_DSN` (use `psql "$PIPELINE_DSN"` after sourcing `.env`). Schemas: `journal`, `news`, `queue`. Read only dashboard role: `dash_reader`.

Rules:
- Always schema qualify: `journal.decisions`, `journal.positions`, `journal.health`. Relying on `search_path` causes intermittent failures.
- Verify columns with `\d journal.<table>` before writing SQL. `journal.decisions` has no `revision` column. C4 decisions use stage `ORDER`, not `EXEC`.
- Migrations are additive only. Next migration number follows the highest in `schema/migrations/`.
- Never run DELETE/UPDATE/TRUNCATE on journal tables without Ian explicitly approving the exact statement.

## Hard rules on timing (market hours)

US market hours are 08:30 to 15:00 America/Chicago, Monday to Friday. Check with `TZ=America/Chicago date` before any restart.
- Never restart `c4-exec`, `c3-gate`, `a3-risk`, `c1-ingestion`, or the llama units during market hours unless Ian explicitly says "do it now, market open". Evening is the normal deploy window.
- Restart only the services a change touches. List them before restarting. Unreviewed changes pulled in by git are out of scope for the current rollout; flag them, do not restart for them.
- Stop `c7-watchdog.timer` before a deploy that restarts several services, restart it after, so you do not send false alarm emails.

## Hard won gotchas (do not relearn these)

- Qwen thinking suppression must be per request: `chat_template_kwargs {"enable_thinking": false}` (`disable_thinking: true` in the agent yaml). Server level flags (`--reasoning-budget 0`) are silently ignored. Correctness requirement, not tuning.
- Any bench of the analyst must use the exact pipeline request shape, including the thinking kwarg; otherwise you measure server defaults.
- Queue spikes are not capacity saturation. The 08:00 CT spike is the open handoff lane's deliberate `available_ts` hold. Diagnose with the `paced_s` vs `starved_s` split before proposing scaling.
- `FakeBroker` order IDs must be globally unique (UNIQUE on `orders.broker_order_id`).
- Any test driving A3 to a SIZE result must drain its own `exec.intent` message or FIFO claim ordering poisons later tests.
- Session timeframe MIP predicates (`close_below_prenews`) evaluate on the post 16:00 ET session close pass only.
- Hard gates run before the discretion model call; no tokens burned under a kill switch.
- `pip install` needs `--break-system-packages` outside the venv. Inside the venv use `.venv/bin/pip`.
- Do not re-run the v0.14.4 Part 7 env append; `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` are already in pipeline.env (want `sudo -n grep -c 'HF_HUB_OFFLINE\|TRANSFORMERS_OFFLINE' /etc/pipeline/pipeline.env` = 2).
- GitHub push failures over HTTPS are hygiene, not deploy blockers. Report them; do not block a rollout on them.
- A git stash from 2026-09-11 holds the pre v0.14.4 files as a safety archive. Do not drop it without asking.
- Known open item as of 2026-09-11: `journal.health` component `risk` heartbeat may be stale. If asked about A3, check `systemctl status a3-risk`, the RISK decision mix, and its journal before trusting discretion sizing.

## How to work

1. **Read before acting.** For any task, first run `git describe --tags`, `git status --short`, and read the most recent `docs/claude_current-state-*.md`. Never rely on this file's version numbers being current.
2. **Plan, then confirm, then do.** For anything that restarts a service, changes config, runs a migration, or pushes: state the plan in a few plain sentences, list every file and service affected, and wait for Ian's yes.
3. **Validate on live Postgres** before packaging: unit tests green, and any new SQL run against the real `journal` schema (read only unless approved).
4. **Version everything.** Each release: bump the version, commit with a clear message, tag `vX.Y.Z`, push tags. Additive migration files, never edits to old ones.
5. **Verify after.** After a restart, confirm `is-active` for each touched unit, tail its journal for errors, and check the relevant `journal.health` heartbeat is fresh.
6. **Rollback path always stated.** Every deploy plan names the previous tag and the exact restart command to go back.
7. **Small fixes get small answers.** For a single error, fix it and say what you fixed in one or two sentences. Full write ups are for releases.
8. Never use dashes or em dashes in prose you write for Ian. Hyphens inside commands, paths and flags are fine.

## Handoff (do this at the end of every substantial session)

Ian keeps a separate design chat that only knows what is uploaded to it. So:
- For each release, write `docs/claude_patch-notes-vX_Y_Z.md` (what changed and why, files touched, services restarted, verification results, rollback).
- At the end of a working session, write `docs/claude_current-state-YYYY-MM-DD.md`: version live, what was done, open items, next steps. Overwrite nothing older; add a new dated file.
- Commit both with the code and tell Ian the filenames so he can upload them to the design chat.
