# Deploy Guide — v0.11.6c (fix broken test import)

**What this does:** captures a one-line fix to a test file
(`tests/unit/test_invalidation_dsl.py`) that was applied by hand on the Spark
but never committed. The committed version imports a module by the wrong name,
which makes the whole `pytest -q` test run fail to start. This fixes that.

**Test-only.** No production code, no database, no service restart. The system's
behaviour is completely unaffected — this only matters when you run the test
suite.

Note: earlier you ran `git checkout -- tests/unit/test_invalidation_dsl.py`,
which reverted the Spark's copy back to the broken committed version. That's
fine — after this release the correct version comes from git and the working
tree is clean.

---

## Part 1 — Get the pack onto your PC

1. Download `v0_11_6c-pack.zip` from the chat.
2. Extract it. You'll get a `v0_11_6c-pack` folder with one `tests` folder and
   two `.md` files.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **tests** folder and the two `.md` files.
3. **One file is REPLACED:** `tests/unit/test_invalidation_dsl.py`.
4. Commit message: `v0.11.6c: fix test_invalidation_dsl import path`
5. Commit, then confirm **3 changed files** (1 replaced + 2 new `.md`).

## Part 3 — Version bump + release

1. `pyproject.toml` → change `version = "0.11.6b"` to `version = "0.11.6c"` →
   commit to `main`.
2. **Releases → Draft a new release** → tag `v0.11.6c` → title
   `v0.11.6c — fix test import` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.11.6c
```

This should switch cleanly (no "local changes" error — you already reverted the
local copy).

## Part 5 — Confirm the working tree is clean and the fix is in

```bash
sudo -u trader git -C /opt/pipeline status --short
grep -n "^from common.invalidation_dsl import" /opt/pipeline/tests/unit/test_invalidation_dsl.py
```

- `status --short` should now show **no ` M` line** for
  `tests/unit/test_invalidation_dsl.py` (the tracked working tree is clean; the
  only remaining entries should be the untracked `?? ops/soak-logs/`,
  `?? src/news_pipeline.egg-info/`, `?? var/`, which are generated artifacts and
  fine to leave).
- The `grep` should print the corrected import line.

Optional — prove the suite now starts cleanly (needs a `trading_test` DB for the
integration tests, but the unit tests run without one):

```bash
sudo -u trader bash -c 'cd /opt/pipeline && PYTHONPATH=src .venv/bin/python tests/unit/test_invalidation_dsl.py'
```

Expect `ALL 20 CHECKS PASSED`.

## Rollback

```bash
sudo -u trader git -C /opt/pipeline checkout v0.11.6b
```

Nothing to restart.
