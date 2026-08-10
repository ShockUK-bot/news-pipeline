# v0.12.23 — A5 bootstrap mode, wide pass, truncation fix (2026-08-10)

## The bug

The thesis store has been empty since it went live. `journal.theses` = **0
rows** from 2026-07-19 to 2026-08-10: three weeks, 17 digests, **113
thesis-lane items in and 113 IGNOREs out**. Not one thesis, ever.

```
 action       | rows
--------------+------
 IGNORE       |  113
 DIGEST       |   17
 REJECT       |    5
 EXPIRED_BULK |    1
```

It is not the timer (fires nightly, verified), not the heavy slot
(`slot=heavy` on every recent run), not the v0.12.4 thinking bug
(`disable_thinking: true` was already set), and not the news supply.

**It is the prompt.** `src/a5_thematic/prompt.py` offers three ops per
item — attach evidence to an *existing* thesis, seed a new thesis, or
ignore — and then instructs: *"New theses are RARE (most nights: zero or
one)."* With an **empty store**, op 1 is physically impossible. The menu
reduces to "do the rare thing" or "ignore", and the model correctly picked
ignore 113 times out of 113. The Phase-8 prompt was written for a healthy
store and has no cold-start mode, so the store could never populate itself.
The model was obeying its instructions perfectly for three weeks.

Corollary that shaped this release: **feeding in more news does not fix
this.** 4x the input yields ~450 IGNOREs and still zero theses. Supply was
never the binding constraint.

Full diagnosis: `claude/diagnose-thesis-store-2026-08-10.md`.

## Changes

### 1. Bootstrap mode — the fix (`src/a5_thematic/prompt.py`)

The prompt is now assembled from a shared core plus one of two mode
blocks, and **code** picks the mode from the store's real population:

- `len(active_theses) < store.bootstrap_min_theses` (3) → **BOOTSTRAP**:
  *"Propose 5 new standing theses... this OVERRIDES any 'new theses are
  rare' discipline... Returning zero new theses tonight is a FAILURE, not
  caution."* Plus a taxonomy of productive drivers (capex cycles, policy
  shifts, physical supply/demand, rate regimes, adoption curves,
  geopolitics) and an explicit instruction that several unrelated headlines
  converging on one driver is the signal to look for.
- at or above the floor → **STEADY**: the original conservative wording,
  byte-for-byte.

The flip is automatic in both directions — seed the store and it reverts
to conservative; expire back below the floor and it re-seeds. Nothing to
remember to turn off.

Also: `ignore` now requires a short reason in `note`, because the operator
reads those to tune this prompt and 113 empty notes told us nothing.

### 2. Wide pass — deep-run starvation (`service.py`, `config/a5.yaml`)

Both recent Sundays logged `deep=True processed=0`. The nightly runs claim
the lane's 1-6 items as they arrive, so the deep pass — 60 items, the run
most likely to author a thesis — has been firing on an empty queue.

New `fetch_wide_items()`: on deep runs only, read the last
`lane.wide_lookback_days` (7) of **ESCALATED** news straight from the news
store, **READ-ONLY**. Never claimed, never acked, never failed, untouched
by the 168h lane expiry — so it cannot race the nightly claims and cannot
be starved by them. Selection reuses A1's own materiality judgement
(`stage='TRIAGE' AND action='ESCALATE'`), newest first.

Wide items may **anchor new theses** and may **receive evidence**. A wide
item the model ignores writes **no journal row** (80 IGNOREs a night would
bury the decision tape) — it is counted in the digest as `wide_ignored`.

Budget is `wide_max_items` MINUS what the lane already contributed:
`ItemOp` list `max_length` is 80 and an over-long list is a hard schema
violation. A unit test pins the config against the schema bound.

### 3. Week context (`service.py`, `lane.wide_context`)

`fetch_week_context()` rides along on deep runs: open positions, the
week's closed trades, and the week's A2 theses — so the long lane can see
what the short lane actually did. Operator request.

### 4. Truncation (`config/a5.yaml`)

`narrative.max_tokens` **3000 → 8000**. All five historical REJECTs are
2026-07-26 and both logged details are truncation, not bad grammar:
`Unterminated string ... (char 8206)` and `Expecting property name ...
(char 8137)`, both dying at line 239-240 — exactly the 3000-token ceiling.
25 items of ops already nearly fills 3000; a 60-item deep pass carrying new
theses cannot fit. `timeout_secs` 600 → 900 for the larger generation
(off-hours oneshot; nothing waits on it).

### 5. Ignore forensics (`service.py`)

Stats now split `ignored_explicit` (the model said ignore) from
`ignored_unaddressed` (the model never mentioned the item), and record
`bootstrap`, `active_before`, `wide_items`, `wide_evidence`,
`wide_ignored`. A second null result is now diagnosable instead of another
three-week guess.

### 6. `--deep` override (`service.py` main)

`--deep`, or `A5_FORCE_DEEP=1`, forces a deep pass on demand. Deploy
verification no longer waits up to six days for the next Sunday, and no
config file has to be edited on the box (an edited `config/` file makes
`git checkout <tag>` refuse to move).

## Files

REPLACED (3): `src/a5_thematic/prompt.py`, `src/a5_thematic/service.py`,
`config/a5.yaml`
NEW (3): `tests/unit/test_a5_bootstrap.py`, `patch-notes-v0_12_23.md`,
`v0_12_23-deploy-guide.md`

**6 changed files** on the upload commit, then the `pyproject.toml` pencil
edit to `0.12.23`.

No migration. No new services, timers, sudoers or env vars. No restarts —
`a5-thematic` is a oneshot timer and reads both the new code and the new
config on its next run.

## Tests

15 new unit tests in `tests/unit/test_a5_bootstrap.py`: bootstrap vs steady
prompt selection (including that "New theses are RARE" cannot survive into
bootstrap mode); singular/plural rendering; the mandatory ignore-note rule
in both modes; bootstrap reminder and `store_status` in the user turn; week
context present only when supplied; deep marker and retry preserved;
`resolve_ops` accepting wide items as anchorable and as evidence targets;
unknown-thesis downgrade still firing on a wide item; **back-compat** —
the old 3-argument `resolve_ops` call behaves exactly as before.

Three of them are regression pins on config rather than code, aimed
squarely at the two bugs in this release: `max_tokens >= 8000`,
`wide_max_items <= ItemOp.max_length (80)`, and a sane bootstrap floor.

Sandbox run against the current repo HEAD (b1a846a, v0.12.22):
**474 passed** with the same 15 pre-existing failures the unmodified tree
produces (missing `pandas_market_calendars` and friends in the sandbox —
identical failure set before and after, verified by diff). A5 subset alone:
**25 passed** (10 existing + 15 new), zero changes needed to the existing
Phase-8 tests.

## What success looks like

The next deep run logs `new_theses=N` with **N > 0** and
`journal.theses` stops being empty. If it is still zero, the new stats say
which wall we hit: `bootstrap=true` with high `ignored_explicit` means the
model read the instruction and still refused (→ the local 122B is not up
to authoring, proceed to the hosted-API pass); high `ignored_unaddressed`
means it silently dropped items (→ pack too large); `wide_items=0` means
the wide query found nothing (→ check A1 ESCALATE volume).

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.12.22
```

Nothing else to undo — no migration, no systemd change, no restart. The
store simply goes back to not populating itself.
