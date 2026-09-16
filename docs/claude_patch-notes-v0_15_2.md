# Patch notes v0.15.2

Heavy slot back on llama.cpp b10064. 2026-09-16 morning. Previous tag: `v0.15.1`.

## Why

Morning checklist after the v0.14.10 cutover: A4 premarket on 09-16 (first run on b10970) took p50 248 s per call against 141 s on 09-14. The server's own timing lines show the cause: the 122B MoE decodes at 7.8 tok/s on b10970 versus 10.6 tok/s on b10064 at the same prompt size (2,280 tokens), a 26 percent regression; today's sheet was also longer (1,875 vs 1,403 tokens). Output validated fine (no invalid or fallback lines), so this is speed only. The analyst (27B dense) is 17 percent faster on b10970 and triage is unchanged, so only the heavy unit rolls back.

## Change

`ops/systemd/llama-heavy.service`: `ExecStart` back to `/opt/llama.cpp/build/bin/llama-server` with `--no-mmap` (the b10064 spelling). Comment records the measurement and how to retry a newer build. Installed with `sudo cp` and `daemon-reload` at 08:50 CT; the unit is stopped between uses, so no restart was needed. First run on the restored build: A7 EOD at 15:35 CT; A4 tomorrow 06:00 CT should be back near 141 s per call.

## Rollback

Reverse the two tokens (path to `build-2026-09/bin`, `--no-mmap` to `--load-mode none`), `cp`, `daemon-reload`.
