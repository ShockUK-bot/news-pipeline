# v0.11.6c — Fix broken import in test_invalidation_dsl.py

Tiny fix. **One file: `tests/unit/test_invalidation_dsl.py`.** Test-only — no
production code, no behavior change, no service restart.

## Why

The committed test imported the MIP module by the wrong name:

```python
from invalidation_dsl import (...)          # bare — no such module
```

The module actually lives at `src/common/invalidation_dsl.py` (the agent code
imports it as `common.invalidation_dsl`). Under the normal test run
(`PYTHONPATH=src`), the bare import raised `ModuleNotFoundError: No module named
'invalidation_dsl'` **during pytest collection**, which aborts the entire
`pytest -q` run ("Interrupted: 1 error during collection"). That quietly
undermined the "run the test suite before deploying" step the deploy guides
recommend.

This exact correction existed as an **uncommitted local edit on the Spark** (it
had been applied by hand and never pushed). This release captures it in git so
the working tree is clean and the suite runs.

## Change

`tests/unit/test_invalidation_dsl.py` (REPLACED) — one line:

```python
-from invalidation_dsl import (ArmContext, Bar, MIPError, STDLIB,
+from common.invalidation_dsl import (ArmContext, Bar, MIPError, STDLIB,
                               compile_predicate, validate)
```

(Note: this file is a standalone check script — helper `ok()` plus module-level
assertions ending in "ALL CHECKS PASSED", no `def test_*` — so pytest collects
zero test items from it. The point of the fix is only that it now *imports*
cleanly and doesn't break collection of the rest of the suite.)

## Validation

- Standalone (`PYTHONPATH=src python tests/unit/test_invalidation_dsl.py`):
  **ALL 20 CHECKS PASSED**.
- `pytest -q tests/unit/` no longer errors during collection on this file
  (21 passed alongside `test_triage_router.py`, previously an immediate
  collection error).

## Rollback

`git checkout v0.11.6b` on the Spark. Nothing to restart — it's a test file.
