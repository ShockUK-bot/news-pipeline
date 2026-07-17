# Patch v0.5.5 — grammar-safe answer schema (fixes the 400 Bad Request)

Diagnosed on the Spark via schema probe: the planner schema converts to a
grammar fine (HTTP 200); the ANSWER schema failed ("failed to parse grammar",
HTTP 400) because `recommendation` and `filing_proposal` were nullable
sub-objects (anyOf + null) — a construct this llama.cpp build can't convert.
A2's schemas only use required sub-objects, which is why A2 was unaffected.

Fix: both sub-objects are now ALWAYS present with explicit sentinels —
`stance: "no_view"` + empty rationale means "no recommendation";
`ticker: ""` means "no filing proposed". Same contract and behavior; the
dashboard payloads are unchanged (a sentinel proposal is converted to
null before storage, so no FILE button appears on non-proposal answers).

## Contents

| File | Action |
|---|---|
| `src/a13_chat/schema.py` | **replace** — sentinel-based Recommendation/FilingProposal, `effective_*()` helpers |
| `src/a13_chat/prompt.py` | **replace** — instructs the model to always emit both objects, with sentinels |
| `src/a13_chat/service.py` | **replace** — uses the `effective_*()` helpers |
| `tests/unit/test_a13_chat.py` | **replace** — sentinel tests + grammar-safety regression test (suite: 22 + 6 wake) |
| `ops/a13-schema-probe.py` | new — pre-deploy probe: both schemas must return HTTP 200 from :8081 |

## Deploy

GitHub: branch `a13-chat-v0.5.5` → upload `src`, `tests`, `ops` folders →
PR (4 modified + 1 new) → merge. Spark:

```bash
sudo -u trader git -C /opt/pipeline pull

# pre-flight: both lines must print HTTP 200 OK
sudo -u trader bash -c 'cd /opt/pipeline && PYTHONPATH=src .venv/bin/python ops/a13-schema-probe.py'

# cap a13-chat's stop timeout while we're here (same wedge as the dashboard had)
sudo sed -i '/^RestartSec=5/a TimeoutStopSec=10' /etc/systemd/system/a13-chat.service
sudo systemctl daemon-reload

sudo systemctl restart a13-chat
```

Verify: ask a fresh question in the CHAT tab (earlier failed ones were
quarantined after retries — don't wait on them). Expect an answer in
~30–90 s. Then the full B6 smoke: "why was <ticker> vetoed?" against the
decision tape, and a filing round-trip on trading_test.
