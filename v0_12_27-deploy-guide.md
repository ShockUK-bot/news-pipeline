# Deploy Guide — v0.12.27 (dashboard thesis chip)

**What this does:** your new thesis trades show a purple **THESIS** chip
on the dashboard instead of wrongly displaying as NEWS. Display-only fix —
the database rows were always correct.

**Risk: none** (one HTML file). **Time: ~5 minutes. When: any time — no
restarts.**

---

## Part 1 — Get the pack

Download `v0_12_27-pack.zip` → **Extract All** into a NEW empty folder.

## Part 2 — Upload to GitHub

1. `github.com/ShockUK-bot/news-pipeline` → **Add file → Upload files**.
2. Drag in the **dashboard** folder and the two `.md` files.
3. **One file is REPLACED:** `dashboard/index.html`.
4. Commit message: `v0.12.27: dashboard thesis origin chip`
5. Confirm **3 changed files**.

## Part 3 — Version bump + release

`pyproject.toml` → pencil icon → `version = "0.12.27"` → commit →
**Releases → Draft a new release** → tag `v0.12.27` → **Publish**.

## Part 4 — Pull onto the Spark

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.12.27
```

That's the whole deploy — the dashboard re-reads the file on every page
load, so nothing restarts.

## Part 5 — Confirm

Refresh the dashboard in your browser (Ctrl-F5 to be sure). The positions
table should now show a purple **THESIS** chip on this morning's entries,
NEWS and SCAN unchanged elsewhere.

If you haven't already sanity-checked the underlying rows:

```bash
sudo -u postgres psql -d trading -c "SELECT ticker, origin, profile FROM journal.positions WHERE status='OPEN' ORDER BY opened_ts DESC;"
```

`origin=thesis, profile=thesis_v1` on the new rows = everything was
always correct underneath; only the label lied.
