# Patch notes v0.14.10

llama.cpp runtime upgrade, unit files only. Built 2026-09-14, deployed 2026-09-15 evening. Previous tag: `v0.14.9`. No model change, no pipeline code change, no config change.

## Change

All three inference units moved from the July and August llama.cpp builds to a fresh build of upstream tag **b10970**:

| Unit | Before | After |
|---|---|---|
| `llama-a1` (triage, :8080, Qwen3.5-9B) | `build/` b10064 | `build-2026-09/` b10970 |
| `llama-a2` (analyst, :8081, Qwen3.8-27B, MTP) | `build-2026-08/` b10573 | `build-2026-09/` b10970 |
| `llama-heavy` (:8084, Qwen3.5-122B-A10B, manual start) | `build/` b10064 | `build-2026-09/` b10970 |

Each unit swaps `--no-mmap` for `--load-mode none` (the old flag was removed upstream; the new binary refuses to start with it). Nothing else in the flags changed. `llama-a2b` (shadow, :8082) is on the same build for future benches.

Build: `/opt/llama.cpp/src-b10970` (git worktree at tag b10970, the August checkout untouched), configured as the August build (Release, CUDA, arch 121, native, no curl), built into `/opt/llama.cpp/build-2026-09/`. The old binaries stay in place as rollback targets. Prerequisite: `/opt/llama.cpp` chowned to `trader` by the operator on 2026-09-14.

## Evidence

- Analyst A/B (`ops/bench_analyst.py`, 20 real news items, exact pipeline request shape, thinking off on both): p50 12.07 s vs 14.62 s (**17 percent faster**), p95 19.97 s vs 22.44 s, identical output size (250 tokens), 20/20 schema on both, 0 empty, 0 errors, 0 magnitude violations. `docs/bench-analyst-b10573-vs-b10970-2026-09-14.txt`.
- Triage smoke (9B on the new binary, spare port, strict JSON): valid, 0 reasoning characters, 65 tokens, 5.3 s cold.
- Heavy smoke (122B on the new binary, spare port, strict JSON): healthy in 90 s, valid, 0 reasoning characters, 65 tokens, 4.5 s. Memory peaked at 112 of 119 GB with all three models loaded, the same shape A4 and A7 produce daily.

Two bench script defects were fixed on the way, both leftovers of the 08-22 reset: the item query used a non existent `revision` column on `journal.decisions`, and endpoint A was hardcoded to skip the thinking off kwarg (so every previous A/B compared a thinking live slot against a non thinking candidate; the first run of this bench showed a bogus 61 percent). Commits `1f5c415`, `8db7acf`.

## Deploy record

2026-09-15, market closed. 15:41 CT `c7-watchdog.timer` paused; `llama-a2` restarted, healthy in 30 s, `/props` b10970-bfdc32183, 4 slots, 32k; `llama-a1` restarted, healthy in 15 s, `/props` b10970, 2 slots; timer resumed. Heavy smoke 15:46 to 15:49 on port 8085, then `llama-heavy.service` installed and `daemon-reload` only (unit stays stopped between uses; A6 nightly at 19:00 CT is its first real run on the new build).

During the analyst reload two `signal.analyst` messages hit 503 and were retried by the queue (attempts 3 of 5), both done by 15:43. Since the cutover: TRIAGE p50 2.9 s (10 rows), ANALYST p50 17.1 s (2 rows), no agent errors beyond those retries. Installed units identical to `ops/systemd/`.

## Rollback

Per unit: path back (`build/` for a1 and heavy, `build-2026-08/` for a2), `--load-mode none` back to `--no-mmap`, `sudo cp`, `daemon-reload`, restart that unit (evening). The old binaries are untouched. Repo: `git reset --hard v0.14.9` on `main`.

## Follow up

- Check `journalctl -u llama-heavy` and the A6 nightly journal after 19:00 CT on 2026-09-15 (first real heavy run on b10970), and A4 at 06:00 CT on 2026-09-16 (premarket sheet, the slowest heavy consumer; p50 was 141 s per call).
- Watch analyst p50 over the week; expected around 15 s in production against 18 s before.
