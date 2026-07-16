# Patch v0.5.3 — wake the analyst model on demand for chat

Off-hours the Analyst llama-server (:8081) may be stopped. Before v0.5.3 a
chat question just sat pending. Now A13 probes the server before answering;
if it's down, it posts "Analyst model is asleep — waking it now…" into the
chat, starts the `llama-analyst` systemd service (passwordless sudo, that one
command only), waits for the model to load (default up to 240 s), and answers
at full quality. If the wake fails, you get a clear ERROR row instead of an
endless pending bubble. During market hours the probe finds the server up and
nothing changes.

## Contents

| File | Action |
|---|---|
| `src/a13_chat/wake.py` | new — probe / wake / wait logic |
| `src/a13_chat/service.py` | **replace** — wake integration, interim SYSTEM note, idempotency fix |
| `config/a13.yaml` | **replace** — new `wake:` section |
| `ops/systemd/llama-analyst.service` | new — TEMPLATE unit; **edit ExecStart before enabling** |
| `ops/sudoers.d/a13-wake` | new — lets `trader` run only `systemctl start llama-analyst.service` |
| `tests/unit/test_a13_wake.py` | new — 6 tests (suite total: 177 + 21) |

## Upload via GitHub browser

Branch `a13-chat-v0.5.3` from `main` → Upload files → drag `src`, `config`,
`ops`, `tests` folders → PR should show **4 new + 2 modified** → merge.

## Spark deploy (one-time setup for the unit + sudoers, then the usual loop)

```bash
sudo -u trader git -C /opt/pipeline pull

# 1. capture how the analyst server is launched today (while it's running):
ps aux | grep llama-server
#    -> copy the FULL :8081 command line

# 2. install the unit and put that command line on its ExecStart= line:
sudo cp /opt/pipeline/ops/systemd/llama-analyst.service /etc/systemd/system/
sudo nano /etc/systemd/system/llama-analyst.service     # replace the ExecStart placeholder
sudo systemctl daemon-reload

# 3. sudoers rule (exactly this way — visudo -c guards against lockout):
sudo cp /opt/pipeline/ops/sudoers.d/a13-wake /etc/sudoers.d/a13-wake
sudo chmod 440 /etc/sudoers.d/a13-wake
sudo visudo -c        # must print "parsed OK"

# 4. restart A13:
sudo systemctl restart a13-chat

# 5. verify the wake path works end-to-end (only if you can stop the analyst
#    right now, i.e. off-hours): stop your current :8081 server, then ask a
#    question in the CHAT tab. Expect the "waking" note, then the answer.
#    Confirm the sudo rule from a trader shell:
sudo -u trader sudo -n /usr/bin/systemctl start llama-analyst.service   # no password prompt
```

## Notes

- From now on, run the analyst via `systemctl start/stop llama-analyst`
  instead of a manual command, so the unit is the single way it launches and
  the wake path always works. If something else already occupies :8081 the
  probe simply finds it alive and never runs the wake command.
- The wake command is config, not model output — A13's LLM cannot choose or
  change what gets executed.
- A13 does NOT stop the model afterwards; your existing off-hours scheduler
  (C7) keeps owning slot swaps. If memory pressure with the Heavy slot
  becomes an issue, add `sudo systemctl stop llama-analyst` to that
  scheduler's swap-in step.
