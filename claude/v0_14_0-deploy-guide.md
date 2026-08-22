# v0.14.0 deploy guide — Qwen3.8-27B shadow slot + analyst bench

**Every command in this guide runs on the SPARK.** Nothing runs on the VPS.
Nothing touches the private dashboard or the public dashboard.

Open a terminal on the Spark the same way you always do (sitting at the
machine, or SSH from your PC). Every command below is typed into that
terminal and followed by Enter. Commands starting with `sudo` will ask for
your password the first time.

**What this guide does NOT change:** no config file, no database, no running
service, no agent. Parts 1–6 are all safe to run while the market is open,
though Part 5 (the bench) is better after the close because it competes for
GPU time. Part 7 (the cutover) is the only part that must wait for the
close, and you only do it if Part 6 says GO.

---

## Part 0 — Check you have room

The new model is about 20 GB, and Part 1 builds llama.cpp into a fresh
directory (~10 GB) so the binary serving live trades is never touched.

```bash
df -h /opt
```

Look at the **Avail** column. You want at least **35G** free. If you have
less, stop and tell Claude — do not start deleting things.

Also confirm where you are starting from:

```bash
git -C /opt/pipeline describe --tags
```

This should print **v0.13.10**. If it prints something else, tell Claude
before continuing.

---

## Part 1 — Update llama.cpp (SAFE DURING MARKET HOURS)

Qwen3.8 needs a newer llama.cpp than you are running. You are on b10064;
the current quants are built against b10419.

**We build into a NEW directory and leave the existing one completely
alone.** The old `build/` folder holds the binary that is serving live
trades right now. Nothing in this Part touches it, which means your rollback
for llama.cpp is "point back at the old folder" — instant, and available
even months from now.

### 1a. Fix the ownership problem first

`/opt/llama.cpp` is owned by root (it was installed with `sudo`), but you
are running these commands as yourself. That is why git said "dubious
ownership" and why cmake said "Permission denied". Take ownership of the
folder once and both problems go away permanently:

```bash
whoami
sudo chown -R "$USER":"$USER" /opt/llama.cpp
git config --global --add safe.directory /opt/llama.cpp
```

The first command just tells you which account you are on — note it down.
The second hands you the folder. The third tells git to stop worrying about
it regardless.

This is safe: the `llama-a1` / `llama-a2` / `llama-heavy` services only ever
**read** the binary, they never write to it, so changing who owns the folder
does not affect them.

### 1b. Check disk space

The second build directory needs roughly 10 GB on top of the 20 GB for the
model.

```bash
df -h /opt
```

You want at least **35G** in the **Avail** column. If you have less, stop
and tell Claude.

### 1c. Pull the new source

```bash
cd /opt/llama.cpp
git pull
git log -1 --oneline
```

`git pull` should now succeed (it will print either "Updating..." or
"Already up to date"). `git log -1 --oneline` prints the commit you are on —
paste that line to Claude if anything later goes wrong.

**If `git pull` still fails**, stop and send Claude the exact error. Do not
continue: without a successful pull you would just rebuild the same old
version.

### 1d. Build into a new folder

```bash
cd /opt/llama.cpp
cmake -B build-2026-08 -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121 -DCMAKE_BUILD_TYPE=Release
cmake --build build-2026-08 --config Release -j"$(nproc)"
```

Note the `build-2026-08` in both commands — that is the new folder, and it
is what keeps this safe during market hours.

This takes 10–20 minutes. Expect a lot of output. These warnings are normal
and can be ignored:

- `ccache not found`
- `NCCL not found`
- `OpenSSL not found, HTTPS support disabled`
- `ARM -march/-mcpu not found, -mcpu=native will be used`
- `Replacing 121 in CMAKE_CUDA_ARCHITECTURES with 121a`

What you are looking for is the **absence** of lines containing `Error`. If
the build ends without any, it worked.

### 1e. Confirm the new binary

```bash
/opt/llama.cpp/build-2026-08/bin/llama-server --version
```

Write the build number down. You want **b10419 or higher**. If it prints
b10064 or similar, the `git pull` in 1c did not actually update anything —
go back and check.

Sanity-check that the old one is still there and untouched:

```bash
ls -l /opt/llama.cpp/build/bin/llama-server
```

That file is what your live trading system is running. It should still
exist, with its original date.

> **From here on, every command in this guide that starts a model server
> uses `/opt/llama.cpp/build-2026-08/bin/llama-server`** — the new binary.
> The live `llama-a1`, `llama-a2` and `llama-heavy` services keep using the
> old one until Part 7.

---

## Part 2 — Download Qwen3.8-27B (SAFE DURING MARKET HOURS)

Same source you used for the current models:

```bash
hf download unsloth/Qwen3.8-27B-GGUF --include "*Q5_K_M*" --local-dir /opt/models/qwen3.8-27b
```

If `hf` says "command not found":

```bash
pip install -U "huggingface_hub[cli]"
```

...then run the download again.

If unsloth does not have a Q5_K_M for this model, use bartowski instead:

```bash
hf download bartowski/Qwen3.8-27B-GGUF --include "*Q5_K_M*" --local-dir /opt/models/qwen3.8-27b
```

Now see exactly what landed:

```bash
find /opt/models/qwen3.8-27b -name "*.gguf" -exec ls -lh {} \;
```

You should see one file of roughly 19–21 GB with a name like
`Qwen3.8-27B-Q5_K_M.gguf`. You may also see a smaller `mmproj-*.gguf` —
that is the vision component. **Ignore it.** We are not using vision, and we
will not pass `--mmproj`, so it will not be loaded.

Link the real file to the fixed path the service expects. **Use exactly the
filename that `find` printed above**, not the one in this example:

```bash
sudo ln -sf /opt/models/qwen3.8-27b/Qwen3.8-27B-Q5_K_M.gguf /opt/models/qwen3.8-27b-q5_k_m.gguf
```

Check the link resolves:

```bash
ls -lL /opt/models/qwen3.8-27b-q5_k_m.gguf
```

If that prints a ~20 GB file, you are good. If it says "No such file or
directory", the filename in your `ln` command did not match what `find`
printed — redo it.

> **Why this matters:** the v0.11.8 heavy-slot outage was exactly this — a
> symlink whose name did not match what llama.cpp expected, failing in half
> a second and silently degrading three agents for three days.

---

## Part 3 — Start the model BY HAND first (do not skip)

This is the most important step in the guide. Four times now this model
family's thinking channel has broken the JSON contract on deploy. We find
out in a terminal, not in a systemd unit at 9:30am.

Start the server in the foreground:

```bash
/opt/llama.cpp/build-2026-08/bin/llama-server \
  -m /opt/models/qwen3.8-27b-q5_k_m.gguf \
  --host 127.0.0.1 --port 8082 \
  -c 32768 \
  --jinja \
  --reasoning-effort low \
  --spec-type draft-mtp \
  -ngl 999 --no-mmap
```

> **VERIFIED ON THE SPARK, 2026-08-21.** Measured on llama.cpp b10573 with
> `unsloth/Qwen3.8-27B-GGUF` UD-Q5_K_M, one thesis-shaped request under a
> strict `json_schema`:
>
> | Configuration | Reasoning | Output | Wall |
> |---|---|---|---|
> | `--reasoning-effort low` | 792 chars | 435 tok | 25.8 s |
> | `--reasoning-budget 0` added | 792 chars | 435 tok | 25.8 s |
> | per-request `enable_thinking: false` | **0 chars** | **78 tok** | **4.9 s** |
>
> Three conclusions, all load-bearing:
>
> 1. **`--reasoning-budget 0` is ignored by this model** — exactly as it was
>    by the 122B in v0.12.4. Do not rely on it.
> 2. **Thinking is suppressed per request, not by a server flag.** The
>    cutover is therefore `disable_thinking: true` in config, using the
>    `LlamaCppBackend` flag that already exists since v0.12.4. No code change.
> 3. **Dropping `--grammar-file` was correct.** A `json_object` request came
>    back fenced with ```` ```json ````, but the strict `json_schema` the
>    pipeline actually sends returned bare, valid JSON with the right enum.
>
> MTP speculative decoding lifted raw decode 11.3 → 21.3 tok/s. Under the
> strict grammar the gain is smaller (~16 tok/s) because the grammar rejects
> draft tokens. That is fine — **wall clock per thesis is the metric**, and a
> model that decodes 2× faster while emitting 3× the tokens is slower.

Leave this terminal running. It will print a lot, then settle. Wait for a
line containing `server is listening`.

**If it exits immediately** with something about an unknown architecture,
the binary is too old — you are running the wrong path, or `git pull` in
Part 1c did not update. Re-check Part 1e.

**If it says "No such file or directory"**, you typed the old `build/` path
instead of `build-2026-08/`.

**Now open a SECOND terminal** on the Spark (new SSH session, or a new
window). In the second terminal:

```bash
curl -s http://127.0.0.1:8082/health
```

Expect `{"status":"ok"}`.

Then the test that actually matters:

```bash
curl -s http://127.0.0.1:8082/v1/chat/completions -d '{
  "messages":[{"role":"user","content":"Reply with a JSON object: {\"ok\": true}"}],
  "response_format":{"type":"json_object"}, "max_tokens":64}' | python3 -m json.tool
```

**PASS** = the `"content"` field contains a small JSON object.

**FAIL cases and what they mean:**

| What you see | What it means | What to do |
|---|---|---|
| `"content": ""` (empty) | Thinking is still on; output went to `reasoning_content` | Stop the server (Ctrl+C in terminal 1), change `--reasoning-effort low` to `--reasoning-effort medium`, retry. If still empty, send Claude the full curl output. |
| An error on **every** request while `/health` says ok | Invalid reasoning-effort value | You typed something other than `low`, `medium` or `xhigh`. `high`, `max` and `minimal` are accepted by llama.cpp but rejected by this model's template. |
| Fenced output with ```` ``` ```` | Server ignored `response_format` | Send Claude the output — we may need to put `--grammar-file` back. |
| `<think>` text inside content | Reasoning leaked | Send Claude the output. Do **not** add `--reasoning-format none` — that is a known-bad flag here (v0.5.8). |

Now the speed check, same shape as a real analyst call:

```bash
time curl -s http://127.0.0.1:8082/v1/chat/completions -d '{
  "messages":[{"role":"user","content":"Headline: Acme Corp receives FDA approval for its lead drug. Reply with JSON containing keys ticker, direction, magnitude_est, reasoning."}],
  "response_format":{"type":"json_object"}, "max_tokens":400}' > /dev/null
```

Note the `real` time. For reference, the live analyst averages ~40 s per
signal at ~12.5 tok/s.

**Leave the hand-started server running** — Part 5 uses it. Go to Part 5 if
you want the numbers tonight; do Part 4 whenever.

---

## Part 4 — Install the service file and push to GitHub

### 4a. On the Spark

```bash
sudo cp ~/v0_14_0-pack/ops/systemd/llama-a2b.service /etc/systemd/system/
sudo systemctl daemon-reload
```

(Adjust the path if you unzipped the pack somewhere else. If you took it via
GitHub instead, the path is `/opt/pipeline/ops/systemd/llama-a2b.service`
after the checkout in 4b.)

**Do NOT run `systemctl enable`.** This slot is manual-start only, by
design. It has `Restart=no` so a crash stays crashed instead of thrashing
the GPU.

To use it instead of the hand-started server later:

```bash
sudo systemctl start llama-a2b     # start the shadow slot
sudo systemctl stop  llama-a2b     # stop it when you are done benching
```

### 4b. On GitHub (in your browser)

1. Go to the `ShockUK-bot/news-pipeline` repo page.
2. Click **Add file → Upload files**.
3. Drag the `ops` folder and the `claude` folder from the unzipped pack into
   the upload area. Folder paths are preserved; nothing is overwritten
   because every file in this pack is new.
4. Commit message: `v0.14.0: Qwen3.8-27B shadow analyst slot + bench`
5. Click **Commit changes**.
6. Right side of the repo page: **Releases → Draft a new release** → tag
   `v0.14.0` on `main` → title `v0.14.0 — Qwen3.8-27B shadow slot + analyst bench` → **Publish**.
7. Back on the Spark, sync the checkout:

```bash
git -C /opt/pipeline fetch --tags
git -C /opt/pipeline checkout v0.14.0
```

This checkout is safe at any time — it adds two files and changes nothing
that is running.

---

## Part 5 — Run the bench (BEST AFTER MARKET CLOSE)

Both model servers need to be up: `llama-a2` on :8081 (the live one, always
running) and the new one on :8082 (either hand-started from Part 3 or via
`sudo systemctl start llama-a2b`).

Check both:

```bash
curl -s http://127.0.0.1:8081/health; echo
curl -s http://127.0.0.1:8082/health; echo
```

Both must print `{"status":"ok"}`.

Now run the bench:

```bash
cd /opt/pipeline
export PIPELINE_DSN='postgresql://trader:trader_dev@127.0.0.1:5432/trading'
python3 ops/bench_analyst.py --n 20
```

(If you have not done Part 4b yet, run it from the unzipped pack instead:
`python3 ~/v0_14_0-pack/ops/bench_analyst.py --n 20`)

It replays 20 real news items the analyst actually processed in the last 14
days through **both** models under the same strict JSON schema, printing a
line per call. Expect it to take 15–30 minutes — it is running 40 model
calls in sequence, deliberately not in parallel, so the timings are clean.

**This is read-only.** It only SELECTs from the database and posts to the
two model servers. It cannot enqueue anything, place an order, or change any
config.

If you want a quick look first, use `--n 5`.

---

## Part 6 — Read the result: GO or NO-GO

At the end the bench prints a SUMMARY table and the GO criteria. All three
must hold for a cutover:

1. **Candidate p50 ≤ live p50.** This is the whole point. If it is not
   faster, there is no reason to swap.
2. **Candidate schema conformance ≥ live.** A faster model that goes off
   contract is worse than a slow one — every violation costs a full retry.
3. **Candidate empty-content = 0 and errors = 0.** Any empty content means
   the thinking channel is still eating the answer. NO-GO, full stop.
4. **Candidate `magnitude_est` violations = 0.** The bench flags any value
   outside 0.0–1.0, i.e. the model answering in percent instead of decimal
   fraction. That is a 100× position-sizing error if it reached A3. This one
   is non-negotiable regardless of how good the latency looks — a hand test
   on 2026-08-21 produced `magnitude_est: 8.5` from a prompt that did not
   state the units, which is why the bench states them and checks.

Also read the `expected_move_window` line. If the candidate produces fewer
violations than the live model, that is throughput you get back on top of
the raw speed gain — that defect alone was ~10% of A2's morning capacity on
2026-08-19.

**Whatever the numbers say, paste the whole SUMMARY block to Claude before
cutting over.** If it is NO-GO you have lost nothing: stop the shadow slot,
the system carries on exactly as it is today.

```bash
sudo systemctl stop llama-a2b        # or Ctrl+C the hand-started server
```

---

## Part 7 — Cutover (AFTER MARKET CLOSE, ONLY IF PART 6 SAID GO)

**Stop here until after 3:00 PM Chicago time.**

### Step 1 — Back up what you are about to change

This backup IS your rollback. Do not skip it.

```bash
mkdir -p ~/rollback-v0_13_10
cp /etc/systemd/system/llama-a2.service ~/rollback-v0_13_10/
cp /opt/pipeline/config/*.yaml ~/rollback-v0_13_10/
ls ~/rollback-v0_13_10
```

### Step 2 — See which config files name the old model

```bash
grep -rn "qwen3.6-27b-q5_k_m" /opt/pipeline/config/
```

Write down the list of files it prints. These are the journal provenance
strings (`decisions.model_id`) — they are how tomorrow's before/after
comparison works.

### Step 3 — Point the live analyst server at the new model

```bash
sudo systemctl stop llama-a2b
sudo sed -i \
  -e 's#/opt/llama.cpp/build/bin/llama-server#/opt/llama.cpp/build-2026-08/bin/llama-server#' \
  -e 's#/opt/models/qwen3.6-27b-q5_k_m.gguf#/opt/models/qwen3.8-27b-q5_k_m.gguf#' \
  -e 's#-c 16384#-c 32768#' \
  -e 's#--reasoning-budget 0 ##' \
  -e "s#--chat-template-kwargs '{\"enable_thinking\":false}'##" \
  -e 's#--grammar-file /opt/llama.cpp/grammars/json.gbnf##' \
  -e 's#--host 127.0.0.1 --port 8081#--host 127.0.0.1 --port 8081 --jinja --reasoning-effort low#' \
  /etc/systemd/system/llama-a2.service
```

Note the first line: this is also where the analyst slot moves onto the new
llama.cpp binary. `llama-a1` (triage) and `llama-heavy` deliberately stay on
the old binary — one changed service at a time.

Now **read the result before restarting anything**:

```bash
cat /etc/systemd/system/llama-a2.service
```

The `ExecStart` line should:

- start `/opt/llama.cpp/build-2026-08/bin/llama-server` (the **new** binary)
- point at `/opt/models/qwen3.8-27b-q5_k_m.gguf`
- be on `--port 8081`
- carry `--jinja --reasoning-effort low -c 32768 -ngl 999 --no-mmap`
- have **no** `--grammar-file`, **no** `--reasoning-budget`, **no**
  `--chat-template-kwargs`

**If it does not look exactly like that, do not restart.** Restore the
backup and send Claude the file:

```bash
sudo cp ~/rollback-v0_13_10/llama-a2.service /etc/systemd/system/
```

### Step 4 — Update the provenance strings

```bash
cd /opt/pipeline
sed -i 's/qwen3.6-27b-q5_k_m/qwen3.8-27b-q5_k_m/g' config/*.yaml
grep -rn "qwen3.8-27b-q5_k_m" config/
```

The second command should list the same files Step 2 printed.

### Step 4b — Turn thinking off for the analyst slot (REQUIRED)

Without this the new model spends ~350 extra tokens deliberating on every
signal and is **slower than what you have today**. This is the single most
important line in the cutover.

`LlamaCppBackend` has carried a `disable_thinking` flag since v0.12.4 — it
is what the heavy blocks of `a4`–`a8.yaml` already use. Add it to every
config whose `model:` block points at **:8081**.

First see which ones those are:

```bash
grep -rln "127.0.0.1:8081" /opt/pipeline/config/
```

For each file that lists, open it and add `disable_thinking: true` inside
the `model:` block — same indentation as the `model_id:` line above it. For
`config/a2.yaml` the result should look like:

```yaml
model:
  backend: llamacpp
  endpoint: "http://127.0.0.1:8081"
  model_id: "qwen3.8-27b-q5_k_m"
  disable_thinking: true
  temperature: 0.0
  max_tokens: 1200
  timeout_secs: 120
  retries_on_invalid: 1
```

Use `nano /opt/pipeline/config/a2.yaml` if you want a simple editor
(Ctrl+O then Enter to save, Ctrl+X to quit).

Then confirm every :8081 config got the flag:

```bash
grep -rn "disable_thinking" /opt/pipeline/config/
```

The count of files here must match the count from the `grep -rln` above,
plus the a4–a8 heavy blocks that already had it.

### Step 5 — Restart

```bash
sudo systemctl daemon-reload
sudo systemctl restart llama-a2
```

Wait about a minute for the weights to load, then:

```bash
curl -s http://127.0.0.1:8081/health; echo
curl -s http://127.0.0.1:8081/v1/chat/completions -d '{
  "messages":[{"role":"user","content":"Reply with a JSON object: {\"ok\": true}"}],
  "response_format":{"type":"json_object"}, "max_tokens":64}' | python3 -m json.tool
```

Same PASS test as Part 3: non-empty `content` holding a JSON object.

Then prove thinking is actually off in the live path — after the agent
restart below, watch one real analyst decision and check its latency:

```bash
psql "$PIPELINE_DSN" -c "SELECT ts, ticker, action, latency_ms FROM journal.decisions WHERE stage='ANALYST' ORDER BY ts DESC LIMIT 5;"
```

`latency_ms` should be **materially below** the ~40000 you have been seeing.
If it is 25000+ per call, Step 4b did not take — the flag is missing or
misindented in a config file.

Then restart the agents that read their model config at startup:

```bash
sudo systemctl restart a2-analyst a3-risk
```

(If those names are wrong on your machine, list them with
`systemctl list-units 'a*' --no-pager` and tell Claude what you see.)

Finally:

```bash
python3 ops/preflight.py
```

Expect **16/16 CLEAN**. Anything else → Part 8.

### Step 6 — Tag the cutover

Do this on GitHub as `v0.14.1` once it is confirmed working, using the same
upload flow as Part 4b (the changed files are `ops/systemd/llama-a2.service`
and the `config/*.yaml` files that Step 4 touched — export them from the
Spark or tell Claude and we will produce them).

---

## Part 8 — Rollback (about five minutes, any time)

Everything old is still on disk.

```bash
sudo cp ~/rollback-v0_13_10/llama-a2.service /etc/systemd/system/
cp ~/rollback-v0_13_10/*.yaml /opt/pipeline/config/
sudo systemctl daemon-reload
sudo systemctl restart llama-a2
sudo systemctl restart a2-analyst a3-risk
```

That restores the old service file, which points at the **old** llama.cpp
binary in `/opt/llama.cpp/build/` — still present and untouched — and the
old Qwen3.6 model. Both halves of the upgrade roll back together, which is
why Part 1 built into a separate folder.

**Do not delete the old model files** until the five-day watch below is
clean.

---

## Part 9 — Watch the first five trading days

The journal does the A/B bookkeeping for you — every decision carries the
`model_id` that produced it.

```bash
psql "$PIPELINE_DSN" -c "SELECT model_id, stage, action, count(*), round(avg(latency_ms)) AS avg_ms, round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)) AS p95_ms FROM journal.decisions WHERE ts > now() - interval '10 days' AND model_id IS NOT NULL GROUP BY model_id, stage, action ORDER BY stage, model_id, action;"
```

The old `model_id` rows are your baseline. What you want to see on the new
one: **lower `avg_ms` and `p95_ms` on ANALYST rows**, and **no increase in
the share of `REJECT` (invalid output) rows**. Some shift in the thesis /
no-trade mix is expected — it is a different model — but a large swing means
the prompt needs retuning, not that the swap failed.

And the number this was all for, the queue wait A2 has journaled since
v0.13.8:

```bash
psql "$PIPELINE_DSN" -c "SELECT ts::date AS day, count(*) AS analyst_decisions, round(avg((payload->>'queue_wait_secs')::numeric)) AS avg_wait_s, max((payload->>'queue_wait_secs')::numeric) AS max_wait_s FROM journal.decisions WHERE stage='ANALYST' AND ts > now() - interval '10 days' AND payload ? 'queue_wait_secs' GROUP BY 1 ORDER BY 1;"
```

If `max_wait_s` stops reaching the 1400-second range that killed the 08-19
scanner signals, the upgrade did its job.

---

## Appendix — Nemotron 3 Super in the heavy slot (scoped, NOT in this pack)

You asked what it would take. Here is the honest scope.

### It fits, and it runs on your stack

NVIDIA-Nemotron-3-Super-120B-A12B is a hybrid Mamba2-Transformer MoE — 120B
total, 12B active. At Q4_K_M it is roughly 65–70 GB, which is *smaller* than
the Qwen3.5-122B-A10B (77 GB) already in the heavy slot, so the off-hours
memory budget improves slightly. Standard GGUFs exist from bartowski,
lmstudio-community and unsloth, and they work with **upstream** llama.cpp.
NVIDIA itself positions it as the DGX Spark agentic model.

### Four gotchas, all documented by people who have done it on GB10

1. **Ollama's GGUF of this model is NOT compatible with upstream
   llama.cpp** — different MoE tensor layout. Use the bartowski /
   lmstudio-community / unsloth GGUF, never an Ollama blob.
2. **Drop the Linux page cache before loading** (`sudo sh -c 'sync; echo 3 >
   /proc/sys/vm/drop_caches'`) or the load can get OOM-killed on a 128 GB
   box.
3. **`-fa` now needs an explicit argument** (`-fa on`); bare `-fa` is a parse
   error on recent builds.
4. **The NVFP4 variant needs a llama.cpp fork** (Salamander). Ignore it —
   take the plain Q4_K_M GGUF.

### The licence is a real decision, not a footnote

It ships under the **NVIDIA Open Model License**, not Apache 2.0 or MIT.
Every model in your loadout today is permissively licensed. That is a
deliberate posture worth keeping or abandoning consciously, not by accident
during a speed upgrade.

### Why I would not do it yet

The heavy slot's actual problem is A4's ranking call producing invalid JSON
on **every** run — `Unterminated string`, consistently around char 3450–3720,
caused by `narrative.max_tokens: 1400` against `top_k: 15`. That is a config
cap, not a model-quality failure. A better model writing into the same
too-small budget produces the same truncated string. It also wastes 3–5
minutes of heavy-model time per run on the failed first attempt.

**Recommended order:** fix `narrative.max_tokens` / `batch_max` / `top_k` /
`late_daily_max` together against A2's *measured* throughput (which the
Qwen3.8 cutover changes, so measure after) → confirm A4 ranking succeeds
first-try → *then* ask whether the heavy model is the constraint. If it is,
Nemotron 3 Super is a one-evening job on the same shape as this guide: new
unit on :8084, download, hand-start smoke test, swap the `model_id` in the
heavy block of `a4/a5/a6/a7/a8.yaml`.

The one thing worth doing now, if you want it, is the download — 65–70 GB
takes a while and costs nothing sitting on disk.
