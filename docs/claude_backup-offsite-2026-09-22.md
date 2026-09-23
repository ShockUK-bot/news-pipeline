# Off site backups (2026-09-22)

Two layers, both nightly, both watched by the watchdog and shown in the evening digest if they fail.

## Layer 1: compact history to GitHub (live now, no action needed)

`journal-extract.timer` at 03:05 CT runs `ops/journal_extract.py`: 20 tables (positions, exits, orders, trade metrics, rollups, proposals, guard ledger, scanner candidates and counterfactuals, gate counterfactuals, theses, sectors, control, audit, config versions, decisions without payloads, outbox subjects, health) as gzipped CSV, about 8 MB, force pushed as one commit to the branch `journal-extract` of the private repo. It replaces itself each night, so the repo does not grow. If the Spark is lost, this is the trading history and every measurement; it is not a full restore.

## Layer 2: full dump to Google Drive (needs one sign in from you)

`backup-offsite.timer` at 02:50 CT runs `ops/backup_offsite.sh`: copies the newest nightly `pg_dump` (about 170 MB) to the folder `spark-pipeline-backups` in your Google Drive and keeps 21 days there (about 3.6 GB of the free 15 GB). Until Google Drive is authorised the job reports `backup_offsite DEGRADED: remote gdrive not configured` in the evening digest and does nothing else.

### Authorising, once, from your own computer (about five minutes)

The sign in has to happen in a browser on a computer you are sitting at; the Spark cannot show you Google's login page.

1. On your computer, download rclone from https://rclone.org/downloads/ (Windows: the "Intel/AMD 64 bit" zip; Mac: the macOS package) and unzip it.
2. Open a terminal in that folder (Windows: type `cmd` in the folder's address bar; Mac: Terminal, then `cd` into the folder) and run:

   ```
   rclone authorize "drive" "eyJzY29wZSI6ImRyaXZlLmZpbGUifQ"
   ```

   The second argument limits rclone to files it creates itself (scope `drive.file`), so it never sees your other Drive files. A browser tab opens; sign in with the Google account whose Drive you want to use and allow access.
3. The terminal then prints a block starting with `{"access_token":` and ending with `}`. Copy that whole block and paste it to Claude Code with the words "here is the Drive token". Claude Code writes it into `~/.config/rclone/rclone.conf` on the Spark (a file only the trader user can read), runs one upload to confirm, and from then on the 02:50 timer does the rest.

If the token ever stops working (Google revokes it, password change), the evening digest shows `backup_offsite DEGRADED` and the same three steps fix it.

## Layer 3, optional: a copy on the computer you use at home

While you are home, a full copy of the last dump onto your own computer is one command from that computer (replace the path with where you want it):

- Windows (PowerShell): `scp trader@192.168.1.101:/home/trader/pipeline-backups/trading-*.dump C:\Backups\`
- Mac or Linux: `scp trader@192.168.1.101:/home/trader/pipeline-backups/trading-*.dump ~/Backups/`

This is a manual step and it only protects against a Spark failure while the home computer survives; layers 1 and 2 cover the rest.

## Restoring

Full restore on a rebuilt box: install Postgres 16, create the `trading` database and the `trader` role, then `pg_restore -d trading trading-YYYYMMDD.dump`. The extract branch restores with `\copy` per table into the schema from `schema/journal-schema.sql` plus the migrations.
