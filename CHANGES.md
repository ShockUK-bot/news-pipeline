# spark-fixes v0.4.1 (deployment-blocking fixes found in Spark-deploy review)

Baseline: v0.4.0 (177/177 green — re-validated from scratch 2026-07-09 on a
fresh PostgreSQL 16 + from-doc repo rebuild before these fixes were authored).
No src/ or schema/ changes; tests unaffected (still 177).

1. **ops/systemd/a3-risk.service, c4-exec.service — path drift (blocking).**
   Phase 4 units pointed at /home/trader/pipeline, .env, and /usr/bin/python3;
   Phase 1–3 units use /opt/pipeline, /etc/pipeline/pipeline.env, and the venv
   interpreter. On a clean /opt/pipeline install, a3-risk and c4-exec crash on
   start (wrong WorkingDirectory, system python without deps). Harmonized to
   the Phase 1–3 convention.

2. **ops/systemd/pipeline-backup.service — same drift (blocking for D5).**
   ExecStart pointed at /home/trader/pipeline/ops/backup.sh. Now
   /opt/pipeline/ops/backup.sh + /etc/pipeline/pipeline.env.

3. **New units: qdrant.service, llama-a1.service, llama-a2.service.**
   RUNBOOK §6 starts these by name but v0.4.0 shipped no such units.
   llama-server flags follow README (8B: -c 8192 --parallel 2; 32B: -c 16384)
   plus Spark-specific -ngl 999 --no-mmap (unified memory: mmap paging hurts;
   full offload is free on GB10). Model paths: /opt/models/*.gguf.

4. **RUNBOOK-s6-erratum-v1_1.md.** Cold start now includes c2-dedup and
   c8-regime (see erratum for the corrected block).

5. **sql/control-init.sql.** Explicit journal.control seed incl.
   max_trades_per_day and block_entries (schema only seeds kill_switch,
   drawdown_breaker, trading_capital; code defaults covered the rest silently).

6. **.env.example** reconstructed (was in the 127-file repo count but not
   embedded in the consolidated doc).

Install: copy ops/systemd/*.service over the repo's, `sudo cp` to
/etc/systemd/system/, `sudo systemctl daemon-reload`. Commit as v0.4.1.
