# Patch notes v0.14.9

Config and unit file only. 2026-09-14. Previous tag: `v0.14.8`. From the model review of the same day.

## Change

Model provenance corrected to what the analyst slot actually serves.

- `model_id: "qwen3.8-27b-q5_k_m"` in `config/a2.yaml`, `a12.yaml`, `a13.yaml` (was the July `qwen3-32b` string), `a4.yaml`, `a5.yaml`, `a6.yaml` (both entries), `a7.yaml`, `a8.yaml`, `risk.yaml`.
- Comments that said "REQUIRED for qwen3.6-27b" now say "the 27B slot model"; header comments record the correction and the rollback string.
- `ops/systemd/llama-a2.service` description names Qwen3.8-27B UD Q5_K_M with MTP draft; `llama-a2b.service` comment no longer claims 3.6 is live.
- `CLAUDE.md` inference line corrected, version line bumped.

## Why

`llama-a2.service` has served `/opt/models/qwen3.8-27b-q5_k_m.gguf` (Qwen3.8-27B UD Q5_K_M, MTP speculative decoding) since v0.14.1 on 2026-08-22. The matching `model_id` updates were in the commit that the 08-22 `git reset` discarded (the same reset that reverted the shorting mode, see v0.14.8), so every config kept the 3.6 string. `model_id` is only a provenance label written to `journal.decisions.model_id`; the model call goes to the endpoint regardless. Effect: every ANALYST, RISK, GUARD, POSITION_REVIEW and A8 row between 08-22 and this restart says `qwen3.6-27b-q5_k_m` but was produced by 3.8. No trading behaviour changes with this release.

Production evidence that 3.8 is what has been running: analyst p50 latency fell from about 39 s (weeks of 08-03 to 08-17) to 18 s (08-24 onward), and the live server reports the 3.8 file on `/props`.

## Also aligned at deploy

`/etc/systemd/system/llama-heavy.service` had drifted from `ops/systemd/llama-heavy.service` (missing the `--chat-template-kwargs '{"enable_thinking":false}'` flag). Harmless in practice, since the per request kwarg is what the agents rely on, but the installed copy is refreshed with `sudo cp` and `daemon-reload` so the two match. `llama-a2.service` installed copy refreshed the same way (description only; no restart of the model server).

## Services to restart

`a2-analyst`, `a3-risk`, `a12-guard`, `a13-chat` (long running readers of the changed files). A4 to A8 are oneshots and pick the new string up on their next run. No llama server restart.

Bundled with the v0.14.8 restart (`a3-risk`, `c3-gate`, `c4-exec`), so one evening restart covers both: `a2-analyst a3-risk a12-guard a13-chat c3-gate c4-exec`.

## Verification

- All six active, journals show `config version active` for the v0.14.9 commit, no tracebacks.
- Next model call from A2 journals `model_id = qwen3.8-27b-q5_k_m`.
- `diff /etc/systemd/system/llama-heavy.service ops/systemd/llama-heavy.service` empty.

## Rollback

`git reset --hard v0.14.8` on `main` and restart the same services. Nothing to undo in the database; rows keep whatever string they were written with.

## Deploy record

(filled in after the restart)
