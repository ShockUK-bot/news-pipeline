# v0.4.8 — Model Loadout Upgrade, direct swap (2026-07-17)

Upgrades the two production model slots IN PLACE and adds the Heavy
(off-hours) slot, per `claude/model-review-2026-07.md`. Ops + config only —
**no Python source changes, no schema changes; the 185 tests are unaffected.**

| Slot | Old | New | Server |
|---|---|---|---|
| Fast / Triage (A1) | Qwen3 8B Q6_K | **Qwen3.5-9B Q6_K** | same `llama-a1` on :8080 |
| Analyst (A2 + A3 discretion) | Qwen3 32B Q5_K_M | **Qwen3.6-27B Q5_K_M** | same `llama-a2` on :8081 |
| Heavy (off-hours, new) | — | **Qwen3.5-122B-A10B Q4_K_M** | new `llama-heavy` on :8084, manual start only |

**Rollout strategy: direct swap after market close.** Ports and service names
don't change — the models behind them do. Parts 1–3 (rebuild + downloads +
backups) are safe to run **while the market is open**; nothing restarts until
Part 4. Part 4 onward happens **after 3:00 PM Chicago time** (market close).
The old model files stay on disk, so rollback (Part 8) takes about five
minutes. The journal stamps every decision with `model_id`, so the
before/after comparison is still recorded automatically — watch it for the
five trading days after the swap (Part 7).

**Expected downtime:** the two model servers are down for roughly 2–5 minutes
during Part 4. Ingestion keeps running (it never talks to the models);
anything triaged in that window fails-safe to the journal per the A1 retry
contract. Doing this after close makes that window a non-event.

---

## Files in this pack

**REPLACED files (overwrite the existing ones):**

- `ops/systemd/llama-a1.service` — model path → `/opt/models/qwen3.5-9b-q6_k.gguf` (same port :8080, all flags unchanged)
- `ops/systemd/llama-a2.service` — model path → `/opt/models/qwen3.6-27b-q5_k_m.gguf` (same port :8081, all flags unchanged)
- `config/a1.yaml` — model_id `qwen3-8b-q6_k` → `qwen3.5-9b-q6_k` (endpoint unchanged :8080; everything else unchanged)
- `config/a2.yaml` — model_id → `qwen3.6-27b-q5_k_m` (endpoint unchanged :8081)
- `config/risk.yaml` — model_id → `qwen3.6-27b-q5_k_m` (endpoint unchanged :8081; capital/limits values unchanged). **Note:** ships with `backend: stub` exactly like the repo copy. If yours is live with `backend: llamacpp`, keep `llamacpp` — Part 5 Step 2 covers this.

**NEW files (did not exist before):**

- `ops/systemd/llama-heavy.service` — heavy slot, :8084 (manual start; no boot enable)
- `MODEL-UPGRADE-v0_4_8.md` — this guide

The model_id strings matter: they're the journal provenance marker
(`decisions.model_id`), which is how you'll compare old vs. new behavior.

---

## Part 0 — Before you start

You need: the Spark powered on, and a terminal on it (same way you deployed
before — either sitting at the machine or SSH from your PC). Every command
below is typed into that terminal and followed by Enter. Commands starting
with `sudo` will ask for your password the first time.

Check free disk space — the two new production models need ~27 GB, and the
optional heavy model another ~77 GB:

```bash
df -h /opt
```

Look at the `Avail` column. You want at least **35G** free (or **115G** if
you're also downloading the heavy model today). If you're short, tell Claude
before continuing.

---

## Part 1 — Update llama.cpp (SAFE DURING MARKET HOURS)

Qwen3.5/3.6 use a new hybrid-attention architecture. Your llama.cpp build
(b9978, from the original deploy) predates it, so the new models would fail
to load. Rebuild from current source (~10–20 minutes):

```bash
cd /opt/llama.cpp
git pull
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121 -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j"$(nproc)"
```

If the build ends without the word `Error`, it worked.

This is safe while trading: the running servers keep using the old binary
they already loaded into memory. The new binary is only picked up at the
Part 4 restart.

---

## Part 2 — Download the new models (SAFE DURING MARKET HOURS)

The quants are pinned from `unsloth`'s GGUF repos (verified to exist on
2026-07-17; the bartowski naming used for Qwen3 doesn't cover the 3.5/3.6
line the same way).

```bash
hf download unsloth/Qwen3.5-9B-GGUF   --include "*Q6_K.gguf"   --local-dir /opt/models/qwen3.5-9b
hf download unsloth/Qwen3.6-27B-GGUF  --include "*Q5_K_M.gguf" --local-dir /opt/models/qwen3.6-27b
```

(If `hf` says "command not found", run `pip install -U "huggingface_hub[cli]"`
first, then retry.)

Now see the exact filenames that landed, and link them to the fixed paths the
service files expect:

```bash
find /opt/models/qwen3.5-9b /opt/models/qwen3.6-27b -name "*.gguf"
```

You should see one file per folder, named like `Qwen3.5-9B-Q6_K.gguf` (~7.5 GB)
and `Qwen3.6-27B-Q5_K_M.gguf` (~19 GB). Link them (if your filenames differ
slightly from these, use exactly what `find` printed):

```bash
sudo ln -sf /opt/models/qwen3.5-9b/Qwen3.5-9B-Q6_K.gguf     /opt/models/qwen3.5-9b-q6_k.gguf
sudo ln -sf /opt/models/qwen3.6-27b/Qwen3.6-27B-Q5_K_M.gguf /opt/models/qwen3.6-27b-q5_k_m.gguf
```

**Heavy model (optional — any time later, it's independent of today's swap):**
~77 GB download.

```bash
hf download unsloth/Qwen3.5-122B-A10B-GGUF --include "*Q4_K_M*" --local-dir /opt/models/qwen3.5-122b
find /opt/models/qwen3.5-122b -name "*.gguf"
```

Big models often arrive split into parts named `...-00001-of-00002.gguf`.
Link the **first part** (llama.cpp finds the rest automatically) — or the
single file if it isn't split:

```bash
sudo ln -sf /opt/models/qwen3.5-122b/<FIRST-FILE-FROM-FIND> /opt/models/qwen3.5-122b-a10b-q4_k_m.gguf
```

---

## Part 3 — Back up what you're about to replace (SAFE DURING MARKET HOURS)

This backup IS the rollback. Don't skip it.

```bash
mkdir -p ~/rollback-v0_4_7
cp /etc/systemd/system/llama-a1.service ~/rollback-v0_4_7/
cp /etc/systemd/system/llama-a2.service ~/rollback-v0_4_7/
cp /opt/pipeline/config/a1.yaml   ~/rollback-v0_4_7/
cp /opt/pipeline/config/a2.yaml   ~/rollback-v0_4_7/
cp /opt/pipeline/config/risk.yaml ~/rollback-v0_4_7/
ls ~/rollback-v0_4_7
```

You should see all five files listed. Also copy this pack onto the Spark the
same way you've applied previous fix packs (unzip it, e.g. into your home
folder as `model-upgrade-v0_4_8/`).

**STOP HERE until the market closes (3:00 PM Chicago).**

---

## Part 4 — Swap the model servers (AFTER MARKET CLOSE)

Install the updated service files and restart onto the new models + new
llama.cpp binary in one step:

```bash
cd ~/model-upgrade-v0_4_8
sudo cp ops/systemd/llama-a1.service /etc/systemd/system/
sudo cp ops/systemd/llama-a2.service /etc/systemd/system/
sudo cp ops/systemd/llama-heavy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart llama-a1 llama-a2
```

(Deliberately NOT starting `llama-heavy` — it's manual-start only, and
nothing calls it yet.)

Give them a minute or two to load the new weights, then check:

```bash
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8081/health
```

Both should print `{"status":"ok"}`. If a server won't come up:
`journalctl -u llama-a1 -n 30 --no-pager`. An error like `unknown model
architecture` means Part 1 didn't complete — redo it. If you can't resolve
it, go straight to Part 8 (rollback) and the system is back on the old
models tonight; nothing is lost.

**Smoke tests — do not skip.** The pipeline depends on server-enforced JSON:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -d '{
  "messages":[{"role":"user","content":"Reply with a JSON object: {\"ok\": true}"}],
  "response_format":{"type":"json_object"}, "max_tokens":32}' | python3 -m json.tool

curl -s http://127.0.0.1:8081/v1/chat/completions -d '{
  "messages":[{"role":"user","content":"Reply with a JSON object: {\"ok\": true}"}],
  "response_format":{"type":"json_object"}, "max_tokens":32}' | python3 -m json.tool
```

PASS = each prints a response where `"content"` is a small JSON object (not
empty, no ```` ``` ```` fences, no `<think>` text). An **empty content field**
would mean thinking mode isn't suppressed — check the unit file still has
`--reasoning-budget 0`.

Then a latency sanity check on the triage slot (budget: ≤2s):

```bash
time curl -s http://127.0.0.1:8080/v1/chat/completions -d '{
  "messages":[{"role":"user","content":"Headline: Acme Corp receives FDA approval for its lead drug. Reply with JSON: {\"material\": true/false, \"reason\": \"...\"}"}],
  "response_format":{"type":"json_object"}, "max_tokens":128}' > /dev/null
```

The `real` time printed should be around 1 second, and must be under 2.

---

## Part 5 — Update the configs (AFTER PART 4 PASSES)

**Step 1 — copy in the new config files:**

```bash
cp ~/model-upgrade-v0_4_8/config/a1.yaml   /opt/pipeline/config/a1.yaml
cp ~/model-upgrade-v0_4_8/config/a2.yaml   /opt/pipeline/config/a2.yaml
cp ~/model-upgrade-v0_4_8/config/risk.yaml /opt/pipeline/config/risk.yaml
```

**Step 2 — only if your risk.yaml was live on llamacpp:** check your backup:

```bash
grep backend ~/rollback-v0_4_7/risk.yaml
```

If that prints `backend: llamacpp`, open the new file with `nano
/opt/pipeline/config/risk.yaml`, change the line `backend: stub` to
`backend: llamacpp`, then press Ctrl+O, Enter, Ctrl+X. If it prints
`backend: stub`, do nothing.

**Step 3 — restart the pipeline services that read these files.** List their
exact names first:

```bash
systemctl list-units --type=service --no-pager | grep -E 'a1|a2|a3|triage|analyst|risk'
```

then restart each one shown, e.g. (names may differ slightly on your box):

```bash
sudo systemctl restart a1-triage a2-analyst a3-risk
```

**Step 4 — preflight:**

```bash
cd /opt/pipeline && python3 ops/preflight.py
```

Expect the usual PRE-FLIGHT CLEAN.

**Step 5 — verify provenance.** Watch the dashboard decision tape: new TRIAGE
rows should show `model_id = qwen3.5-9b-q6_k` and ANALYST rows
`qwen3.6-27b-q5_k_m`. Once you see that, the swap is complete.

---

## Part 6 — Put v0.4.8 on GitHub (browser, same as v0.4.3)

1. Go to your repo page: `github.com/ShockUK-bot/news-pipeline`.
2. Click **Add file → Upload files**.
3. Drag in, from the unzipped pack: the `ops` folder, the `config` folder, and
   the loose file `MODEL-UPGRADE-v0_4_8.md`. (Folder paths are preserved; the
   five replaced files overwrite the existing ones, the heavy unit and the
   guide are added.)
4. Commit message: `v0.4.8: model loadout upgrade (Qwen3.5-9B / Qwen3.6-27B / heavy slot)`.
5. Click **Commit changes**.
6. Right side of the repo page: **Releases → Draft a new release** → tag
   `v0.4.8` on `main` → title `v0.4.8 — model loadout upgrade` → **Publish**.
7. On the Spark, sync the checkout so git matches what's running:

```bash
git -C /opt/pipeline fetch --tags
git -C /opt/pipeline checkout v0.4.8
```

Do step 7 only AFTER Part 5 — checking out v0.4.8 also brings the new config
files.

---

## Part 7 — Watch the first 5 trading days

The journal did the A/B bookkeeping for you: every decision row carries the
model_id that produced it. Each day, glance at:

1. **Dashboard decision tape** — latency on TRIAGE/ANALYST rows same or
   better than before; no burst of REJECT rows.
2. **Before/after numbers** (run in psql):

```sql
SELECT model_id, stage, action, count(*)
FROM decisions
WHERE ts > now() - interval '10 days' AND model_id IS NOT NULL
GROUP BY model_id, stage, action
ORDER BY stage, model_id, action;
```

The old model_ids are your baseline (the days before today); the new ones
should NOT show a materially higher share of REJECT (invalid output) rows,
and the ESCALATE vs DISCARD mix shouldn't swing wildly (some change is
expected — it's a smarter model — but triage escalating 3× as much would mean
the prompt needs retuning). Anything looks off → Part 8, and bring the
numbers to Claude.

---

## Part 8 — Rollback (any time, ~5 minutes)

Everything old is still on disk. To fully revert:

```bash
sudo cp ~/rollback-v0_4_7/llama-a1.service /etc/systemd/system/
sudo cp ~/rollback-v0_4_7/llama-a2.service /etc/systemd/system/
cp ~/rollback-v0_4_7/a1.yaml   /opt/pipeline/config/a1.yaml
cp ~/rollback-v0_4_7/a2.yaml   /opt/pipeline/config/a2.yaml
cp ~/rollback-v0_4_7/risk.yaml /opt/pipeline/config/risk.yaml
sudo systemctl daemon-reload
sudo systemctl restart llama-a1 llama-a2
sudo systemctl restart a1-triage a2-analyst a3-risk
```

(Same caveat on service names as Part 5 Step 3.) The rebuilt llama.cpp binary
runs the old Qwen3 GGUFs fine, so nothing else needs undoing. If you roll
back after uploading to GitHub, tell Claude — we'll cut a v0.4.9 that reverts
the repo too, so git and the Spark stay in sync.

**Do NOT delete the old model files** (`/opt/models/qwen3-8b-q6_k.gguf`,
`qwen3-32b-q5_k_m.gguf` and the files they point to) until the 5-day watch
is clean. After that, reclaiming the ~30 GB is optional:

```bash
rm /opt/models/Qwen_Qwen3-8B-Q6_K.gguf /opt/models/Qwen_Qwen3-32B-Q5_K_M.gguf
rm /opt/models/qwen3-8b-q6_k.gguf /opt/models/qwen3-32b-q5_k_m.gguf
```

---

## Heavy slot usage (off-hours only, whenever you're ready)

Nothing in the pipeline calls :8084 yet; it's there for A4/weekend
meta-review work as those phases land. Start it by hand when needed, stop it
before the next market open:

```bash
sudo systemctl start llama-heavy     # evenings/weekends; it's on :8084
sudo systemctl stop llama-heavy      # before market open
```

Memory note: heavy (~77 GB) + the two production servers (~27 GB) + KV cache
fits in 128 GB, but it's the one combination with limited headroom — that's
why the unit has no boot-enable and why it must never run during market hours.

## Memory budget (for reference)

| State | Resident | Approx. weights |
|---|---|---|
| Production after swap | 9B + 27B | ~27 GB |
| Off-hours with heavy running | 9B + 27B + 122B-A10B | ~104 GB (tight; by design manual) |

KV cache on the new models is smaller per token than the old ones (hybrid
linear attention) — the budgets above are conservative.
