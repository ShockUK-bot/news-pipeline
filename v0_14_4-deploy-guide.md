# Deploy Guide — v0.14.4 (heartbeats, watchdog, outage alerting)

**What this release does:** makes it impossible for a service to be dead for
three days without you being told. It fixes the huggingface call that hung
a1-triage during the 27 August reboot, gives three services the heartbeat they
never had, teaches the C7 watchdog to check heartbeats instead of just asking
systemd, and puts outages at the top of the morning email instead of the
bottom.

**No trading logic, gates, sizing or execution behaviour changes.**

**When:** any time for most of it, but **c4-exec must be restarted with the
market closed** (Part 7). Doing the whole thing after 15:00 CDT is simplest.

**How long:** about 25 minutes.

---

## Part 1 — One-time git repair, then confirm GitHub and the Spark agree

On 2026-08-31 `git fetch` failed on the Spark with
`cannot open '.git/FETCH_HEAD': Permission denied`, and plain `git` commands
as trader failed with "dubious ownership". Something inside `.git` is owned
by the wrong user, and Part 6 cannot work until that is repaired. This fixes
it permanently, and after it no git command in this or future guides needs
the `-c safe.directory` workaround:

```bash
sudo chown -R trader: /opt/pipeline/.git
sudo -u trader -H git config --global --add safe.directory /opt/pipeline
```

Now confirm what is where. All read-only:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags origin
sudo -u trader git -C /opt/pipeline tag --list 'v0.14.*'
sudo -u trader git -C /opt/pipeline diff --stat HEAD origin/main
sudo -u trader git -C /opt/pipeline status --short
```

**What you want to see:** the fetch completes without a permission error; the
tag list ends at `v0.14.3`; the diff prints **nothing**; and status shows
only `??` lines for `ops/soak-logs/`, `src/news_pipeline.egg-info/` and
`var/` (untracked local files — harmless).

**STOP and tell me if:** the tag list shows `v0.14.4` or higher (this
release's number is taken and I will renumber it), or the diff lists any
files (GitHub holds work this pack was not built on, and uploading would roll
it back — send me the list before touching GitHub).

## Part 2 — Silence the watchdog for the duration

```bash
sudo systemctl stop c7-watchdog.timer
```

**Why:** the new config starts expecting heartbeats from c2-dedup, a3-risk and
a13-chat the instant the code lands, but those three only begin sending them
when they are restarted a few minutes later. Without this, the watchdog emails
you three false alarms in the gap. You will turn it back on in Part 9.

## Part 3 — Get the pack onto your PC

1. Download `v0_14_4-pack.zip` from this chat.
2. Right-click it, choose **Extract All**, into a NEW empty folder.
3. You will get four folders (`src`, `config`, `ops`, `tests`), the file
   `pyproject.toml`, and two `.md` files.

**About `pyproject.toml`:** the copy on your Spark AND on GitHub is broken —
it reads `version = "0.14.0` with no closing quotation mark, which makes the
file invalid, and it has been that way since the v0.14.0 release. It does not
affect the running services, which is why four releases shipped over it. The
copy in this pack is repaired and already says `version = "0.14.4"`, so
**there is no separate version-bump step this time. Do not edit it.**

## Part 4 — Upload to GitHub

1. Go to `github.com/ShockUK-bot/news-pipeline`, then **Add file → Upload
   files**.
2. Drag in all four folders (**src**, **config**, **ops**, **tests**), the
   **pyproject.toml** file, and both **`.md`** files.
3. **Fourteen files are REPLACED** (GitHub handles this automatically):
   - `src/c1_ingestion/heartbeat.py`
   - `src/c2_dedup/service.py`
   - `src/a3_risk/service.py`
   - `src/a13_chat/service.py`
   - `src/c7_watchdog/service.py`
   - `src/c4_exec/deadman.py`
   - `src/a8_briefing/facts.py`
   - `src/a8_briefing/render.py`
   - `config/watchdog.yaml`
   - `config/deadman.yaml`
   - `ops/systemd/a1-triage.service`
   - `ops/systemd/a2-analyst.service`
   - `ops/systemd/c2-dedup.service`
   - `pyproject.toml`

   **Two files are NEW:**
   - `src/common/health.py`
   - `tests/unit/test_health_freshness.py`

4. Commit message:
   `v0.14.4: heartbeats + watchdog freshness + outage alerting`
5. **Commit changes**, then open the commit and confirm it shows **18 changed
   files** (14 replaced + 2 new + 2 `.md`). A different number means something
   did not upload. Stop and tell me.

## Part 5 — Publish the release

1. **Releases → Draft a new release**
2. Tag: `v0.14.4`
3. Title: `v0.14.4 — heartbeats, watchdog, outage alerting`
4. **Publish release**

## Part 6 — Pull it onto the Spark

Back in your SSH window:

```bash
sudo -u trader git -C /opt/pipeline fetch --tags
sudo -u trader git -C /opt/pipeline checkout v0.14.4
sudo -u trader git -C /opt/pipeline describe --tags
```

The last line should print `v0.14.4`.

Part 1's repair means no "dubious ownership" complaints here. If one
appears anyway, rerun the two repair lines from Part 1.

## Part 7 — Install the three unit files and restart

The unit files in the repo are not the ones systemd uses, so they have to be
copied across:

```bash
sudo cp /opt/pipeline/ops/systemd/a1-triage.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/a2-analyst.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/c2-dedup.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Confirm the offline setting actually landed:

```bash
sudo systemctl show a1-triage -p Environment | tr ' ' '\n' | grep -i HF
```

You want to see `HF_HUB_OFFLINE=1`. If nothing prints, the copy or the
`daemon-reload` did not take, so repeat this part before going on.

Now restart. The first five are safe at any time:

```bash
sudo systemctl restart c2-dedup a3-risk a13-chat a1-triage a2-analyst
sleep 30
systemctl is-active c2-dedup a3-risk a13-chat a1-triage a2-analyst
```

All five should print `active`.

**Then c4-exec, with the market closed.** It is the execution engine and it
reconciles your broker positions when it boots, so this is the one restart
that wants a quiet moment:

```bash
sudo systemctl restart c4-exec
sleep 20
systemctl is-active c4-exec
sudo journalctl -u c4-exec -n 20 --no-pager | grep -i reconcil
```

You want `active` and a reconciliation line.

## Part 8 — Check that the heartbeats are real

This is the whole point of the release, so do not skip it. Wait about two
minutes after the restarts, then:

```bash
pg -c "
SELECT component, status,
       round(extract(epoch from (now() - updated_ts))/60) AS age_min,
       left(detail,45) AS detail
FROM journal.health
WHERE component IN ('triage','dedup','risk','chat','analyst','gate',
                    'exec','guard','deadman','ingestion')
ORDER BY age_min DESC;"
```

(If `pg` is not defined in this window, paste the helper again:
`pg() { sudo -u trader bash -c 'set -a; . /etc/pipeline/pipeline.env; set +a; exec psql "$PIPELINE_DSN" "$@"' pg "$@"; }`)

**Every one of those ten must show `age_min` of 0 or 1.** Before this release
`dedup`, `risk` and `chat` sat at 5595. If any of them is still climbing past
2, that service did not pick up the new code — tell me which.

Then run the test suite:

```bash
cd /opt/pipeline && sudo -u trader env PYTHONPATH=src PIPELINE_DSN= \
  /opt/pipeline/.venv/bin/python -m pytest tests/unit/test_health_freshness.py -q
```

Expect `25 passed`. If you want the whole suite, drop the filename — the two
long-standing failures noted in the 16 August review are expected and
unrelated.

## Part 9 — Turn the watchdog back on and read its first pass

Run one pass by hand first, so you see the findings before they become an
email:

```bash
sudo systemctl start c7-watchdog.service
sleep 15
sudo journalctl -u c7-watchdog -n 30 --no-pager
```

**What you want:** a `watchdog pass` line with `findings=0`.

**What you might legitimately see:** this release adds `macro-fetch`,
`thesis-entry` and `a4-late` to the list of timers being checked, and they
have never been checked before. If any of them is not installed or not
enabled, you will get a `TIMER_DOWN` or `UNIT_NOT_FOUND` finding. That is the
watchdog doing its job on day one, not a fault in the release. **Paste the
output to me and I will tell you which are real.**

You may also see a line about orphan health rows being deleted. That is
`ingestion:testsource`, the July test row that had been reading OK for 47.8
days, being cleaned up. That is expected and wanted.

Then re-arm the timer:

```bash
sudo systemctl start c7-watchdog.timer
systemctl is-active c7-watchdog.timer
```

## Part 10 — Belt as well as braces

Add the offline setting to the shared environment file too, so any service I
have not individually covered is protected:

```bash
sudo cp /etc/pipeline/pipeline.env /etc/pipeline/pipeline.env.bak-20260831
echo 'HF_HUB_OFFLINE=1' | sudo tee -a /etc/pipeline/pipeline.env
echo 'TRANSFORMERS_OFFLINE=1' | sudo tee -a /etc/pipeline/pipeline.env
```

The first line takes a backup first. These are the same values already in the
three unit files, so setting them twice changes nothing and costs nothing.

## Part 11 — Tomorrow morning

The 07:35 briefing is the real test. If everything is healthy, the subject
line looks as it always has and the SYSTEM block near the bottom says all
components are OK.

If anything is wrong, the subject line will start with `[OUTAGE]` and the
first thing in the email, above the narrative, will be the component name, how
long it has been silent in human units, and the exact command to fix it.

---

## If something goes wrong

**A service will not start after the restart.** Look at why:

```bash
sudo systemctl status <name> --no-pager
sudo journalctl -u <name> -n 40 --no-pager -l
```

If a1-triage, a2-analyst or c2-dedup fails with something about a model not
being found, the offline setting has exposed a cache that is not where I
expected it. Undo just that part and tell me:

```bash
sudo sed -i '/HF_HUB_OFFLINE\|TRANSFORMERS_OFFLINE/d' /etc/pipeline/pipeline.env
sudo systemctl restart a1-triage a2-analyst c2-dedup
```

**Full rollback:**

```bash
sudo -u trader git -C /opt/pipeline checkout v0.14.3
sudo cp /opt/pipeline/ops/systemd/a1-triage.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/a2-analyst.service /etc/systemd/system/
sudo cp /opt/pipeline/ops/systemd/c2-dedup.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart c2-dedup a3-risk a13-chat a1-triage a2-analyst c4-exec
```

No database changes were made, so there is nothing to undo there.
