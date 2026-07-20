# v0.11.3 — A1 triage: force full-field emission (fixes missing decision-tape tickers)

Found from this session's "no tickers on the C6 tape" investigation. This is
the real root cause of that issue (and, as a side effect, of the "no vetoed
trades" observation from the same day — no ticker means the item never
reaches C3 Gate, so Gate has nothing to veto).

## Symptom

Ticker fill rate on `TRIAGE | ESCALATE` decisions was 90–99.8% every day
2026-07-14 through 2026-07-17, then dropped to 0% on 07-18, stayed at 0% on
07-19, and was only 1.5% on 07-20 as of this investigation. The drop lines up
almost exactly with the 07-17 16:14 CDT restart that shipped the Qwen3.5-9B
model swap (v0.5.7) and the `enable_thinking:false` llama-server flag
(v0.5.8) together.

## Root cause

Confirmed live on the Spark with two manual test calls against the running
`llama-a1` endpoint, using the pipeline's own `prompt.py`/`schema.py` code
(not a synthetic smoke test):

- With a feed-tagged symbol hint in the item (`"symbols": ["ACME"]`), the
  model correctly returned every field, including `"tickers": ["ACME"]`.
- With `"symbols": []` — which is how **every** real RSS/EDGAR item is
  normalized before it ever reaches A1 (`normalize.py` always sets
  `symbols=[]`) — the model's response body contained **only** `material`,
  `confidence`, and `reason`. `tickers`, `direction_hint`, `urgency`, and
  `novelty_score` were not just empty, they were absent from the JSON
  entirely.

`TriageOutput` (`src/a1_triage/schema.py`) gave those four fields Python-side
defaults (`tickers: list[str] = Field(default_factory=list, ...)`, etc.).
Pydantic's `model_json_schema()` — which is sent to llama-server as the
grammar constraint — only lists fields *without* a default as "required."
So those four fields were never actually required by the grammar; the model
has always been technically allowed to skip them. `confidence` got the
opposite treatment back in v0.4.7 specifically to force its emission, but
the other four fields were never given the same protection, and it didn't
matter — until the 07-17 model/llama.cpp-build swap changed how the grammar
engine enforces "required vs optional" during constrained decoding, and the
new setup takes the shortcut the schema always technically allowed: answer
what's required, skip the rest when less certain.

Because the skipped fields had defaults, `validate_triage()` never saw this
as invalid — Python just quietly filled in `tickers=[]`, `direction_hint=
"unclear"`, etc., making a *skipped* answer look identical to a *deliberate*
"no ticker" verdict. That's why this looked like normal quiet-market noise
at first glance, and only showed up as a hard behavioral break once checked
day-by-day.

**This is a schema contract gap, not a broken model.** The model can produce
correct tickers (shown by the first test) — it just wasn't being forced to
try on harder items, and nothing was flagging it when it didn't.

## Fix

`src/a1_triage/schema.py` — removed the Python-side defaults from
`tickers`, `direction_hint`, `urgency`, and `novelty_score`, matching the
same "REQUIRED, no default" pattern already used for `confidence`. Verified
locally: the generated JSON schema's `required` list now includes all seven
fields; a fully-populated response still parses exactly as before; an
incomplete response (the exact shape the live model just produced in
testing) now correctly raises `ValidationError` instead of silently
defaulting.

This does **not** force the model to guess a ticker — `tickers: []`,
`direction_hint: "unclear"`, and a low `novelty_score` remain perfectly
legal values. It only forces the model to actually commit to an answer for
every field instead of dropping one silently.

## What this changes downstream (please watch for a day)

A1 already retries once with the validation error appended to the prompt
before giving up (`src/a1_triage/triage.py`, untouched by this patch) and
journaling `TRIAGE | REJECT` with the raw model output attached. That retry
path exists precisely for cases like this — it just never fired before,
because incomplete output wasn't being treated as invalid. After this
deploys, it will fire correctly:

- Best case: the retry (with the explicit "field required" error appended)
  is enough to get the model to actually answer, and the ticker fill rate
  recovers toward the 90–99% baseline.
- Possible case: some items still fail on the retry too, and now show up
  honestly as `TRIAGE | REJECT` rows in the journal instead of silently
  sailing through with a blank ticker. That's a feature, not a regression —
  but if REJECT volume climbs noticeably in the day after deploy, that's a
  sign this specific model needs a prompt tweak too (not just the schema
  fix), and worth telling me about rather than assuming something broke.

## Changed files

- `src/a1_triage/schema.py` (one class definition changed; `validate_triage`,
  `TriageValidationError`, `triage_json_schema` unchanged)

## Tests

Not run from this session (no repo access from here — private repo, and no
model server to test against outside the Spark). The repo's own suite
(`tests/unit/test_triage_v047.py` and friends) exercises this schema
directly and should be run on the Spark before or right after deploying —
see the deploy guide's optional Part 3.5. This is a schema-tightening
change (fields that were previously optional-with-default become
required-with-no-default); any test that was constructing a `TriageOutput`
without explicitly setting all seven fields would need updating, but the
stub backend's canned responses (`src/a1_triage/backends.py`) already emit
every field, so no source change was expected to be needed there.

## Rollback

```
sudo -u trader git -C /opt/pipeline checkout v0.11.1
sudo systemctl restart a1-triage
```

Nothing else to undo — this is a single-file, code-only change to A1's
output contract. No database, no config, no model-server changes.
