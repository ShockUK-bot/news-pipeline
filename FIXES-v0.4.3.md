# v0.4.3 — Spark deployment fixes (2026-07-12)

Deployment-blocking issues found on DGX Spark (llama.cpp build b9978) during
the v0.4.2 install. No Python source changed; ops config + template only.
Verified on hardware: ops/preflight.py → 16/16 PRE-FLIGHT CLEAN.

## Changed files

### ops/systemd/llama-a1.service, ops/systemd/llama-a2.service
Added to llama-server flags:

    --reasoning-budget 0 --grammar-file /opt/llama.cpp/grammars/json.gbnf

Why:
1. `--reasoning-budget 0` — Qwen3 is a thinking model; by default it spends
   its token budget on private reasoning and returns an EMPTY `content`
   field. Preflight's JSON round-trip (and any agent call) fails.
2. `--grammar-file json.gbnf` — llama.cpp b9978 silently ignores
   `response_format: {"type":"json_object"}` (fenced/free-text output was
   observed). The design requires server-side grammar-enforced JSON
   ("models propose / code disposes"). A server-level default JSON grammar
   restores that guarantee; per-request `json_schema` still overrides it
   with something stricter.

### env.example
- `EDGAR_APP_NAME` value quoted — unquoted spaces break shell sourcing
  (`set -a; source pipeline.env`).
- Documented: DB password letters/digits only ($, %, @, :, / break the DSN
  and/or shell).
- Added the three required-but-missing DASH_* entries (commented).

## How to apply on GitHub (browser)
1. Unzip. On the repo page: Add file → Upload files.
2. Drag the `ops` folder AND the two loose files (`env.example`,
   `FIXES-v0.4.3.md`) into the upload area — folder paths are preserved and
   existing files at the same paths are replaced.
3. Commit message: "v0.4.3: Spark ops fixes (llama flags, env.example)".
4. Releases → Draft a new release → tag `v0.4.3` on main → Publish.

## How to apply on the Spark
Already applied live in /etc/systemd/system. To sync the repo checkout:
    git -C /opt/pipeline fetch --tags
    git -C /opt/pipeline checkout v0.4.3
(code is byte-identical to v0.4.2 except these ops files; 185/185 tests
unaffected). Record v0.4.3 as the soak tag in the soak log.
