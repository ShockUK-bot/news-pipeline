# v0.11.8 — Heavy-slot unit fix (into the repo) + dead-man marketdata off-hours

Two small fixes. No trading-logic changes.

## 1. `ops/systemd/llama-heavy.service` — point `-m` at the real split file

The heavy off-hours model (`Qwen3.5-122B-A10B` Q4_K_M, a 3-part split) had been
**failing to load since it was staged on 2026-07-17** — every time A4 (07:00),
A5 (21:30), or A7 tried to start it on `:8084`, llama.cpp died in ~0.5s with
`invalid split file name`, and those agents silently fell back to the smaller
analyst model for pre-market ranking, the thematic digest, and reports.

Cause: the unit's `-m` pointed at a **symlink** (`qwen3.5-122b-a10b-q4_k_m.gguf`)
whose name lacks the `-00001-of-00003` pattern llama.cpp needs to locate the
sibling shards. (Part 1 being only ~10 MB was a red herring — that's the genuine
first-shard size; the three parts total ~77 GB, matching the model.) Fix: point
`-m` directly at
`/opt/models/qwen3.5-122b/Q4_K_M/Qwen3.5-122B-A10B-Q4_K_M-00001-of-00003.gguf`.

Confirmed on the Spark: the model now loads all three shards (~84s) and serves
on `:8084` (`model loaded` / `listening on http://127.0.0.1:8084`).

The Spark's active unit was already hand-corrected during diagnosis; **this
change updates the repo copy** so a future `git checkout` / re-`cp` doesn't
reintroduce the symlink path.

## 2. `src/c4_exec/deadman.py` — marketdata alert is now RTH-only

After v0.11.7 (which correctly wired the dead-man to monitor everything), the
dead-man went yellow every night on `stale: marketdata` — because off-hours
there are no live quotes, so nothing refreshes marketdata's heartbeat and it
ages until the next open. That's expected, not actionable, and it recreated the
alert-fatigue we'd just cleared on the gate.

Fix: the dead-man now **skips the marketdata alert when out of session**. Its
block/suspend escalations were already RTH-gated, so this only silences the
off-hours *alert*. During RTH, stale marketdata still alerts **and** blocks
exactly as before. Every other component (ingestion, gate, triage, analyst) runs
24/7 and still alerts around the clock. Net effect: the dead-man reads green when
the market's closed, so a genuine alert stands out.

## Validation

Heavy model verified loading + serving on the Spark. Dead-man/exec suite
(`test_risk_exec_flow`, `test_exit_engine_flow`) **22 passed** — all RTH
(`in_session=True`) paths unchanged. `deadman.py` compiles.

## Deploy notes

- `llama-heavy.service`: **no restart needed** — the Spark's active unit is
  already correct (same `-m` path); this only syncs the repo.
- `deadman.py`: activated by restarting **`c4-exec`**, best at **market close**
  (it's the execution engine, reconciles on boot).

## Rollback

`git checkout v0.11.7` and restart `c4-exec`. (The heavy-slot `-m` path on the
Spark stays fixed regardless — it's in `/etc/systemd/system`, edited directly.)
