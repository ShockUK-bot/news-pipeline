"""C5 Mailer (Phase 6) — the dumb sender. Oneshot, fired by c5-mailer.timer
every 5 minutes.

Contract (baseline §7): agents write rendered emails to journal.outbox; this
script sends QUEUED rows and marks them SENT/FAILED. It is the ONLY process
that references the SMTP credentials file (/etc/pipeline/mailer.env, loaded
by its systemd unit alone — no agent-readable context, baseline rule 22
discipline). Recipients come from that env file too, never from the outbox
row — a compromised or confused agent cannot address email anywhere.

Env (all required to actually send):
  MAILER_SMTP_HOST   e.g. smtp.gmail.com
  MAILER_SMTP_PORT   e.g. 465 (SSL) or 587 (STARTTLS)
  MAILER_SMTP_USER   the sending account
  MAILER_SMTP_PASS   app password
  MAILER_FROM        e.g. "Trading Pipeline <you@gmail.com>"
  MAILER_TO          comma-separated recipients
"""
from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate

from common.db import get_pool
from common.log import get_logger, kv
from c1_ingestion.heartbeat import set_health

log = get_logger("c5.mailer")

BATCH_LIMIT = 20
MAX_ATTEMPTS = 5
HEALTH_COMPONENT = "mailer"


class SmtpTransport:
    """Real SMTP. Kept tiny and synchronous (smtplib) — a oneshot sending a
    handful of mails does not need async I/O."""

    def __init__(self, env=os.environ):
        self.host = env.get("MAILER_SMTP_HOST", "")
        self.port = int(env.get("MAILER_SMTP_PORT", "465"))
        self.user = env.get("MAILER_SMTP_USER", "")
        self.password = env.get("MAILER_SMTP_PASS", "")
        self.mail_from = env.get("MAILER_FROM", self.user)
        self.mail_to = [a.strip() for a in env.get("MAILER_TO", "").split(",")
                        if a.strip()]

    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.mail_to)

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.mail_from
        msg["To"] = ", ".join(self.mail_to)
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(body)
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port,
                                  context=ssl.create_default_context(),
                                  timeout=30) as s:
                s.login(self.user, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(self.user, self.password)
                s.send_message(msg)


async def process_outbox(transport=None) -> dict:
    """One pass: send QUEUED rows oldest-first. Returns counters (tested)."""
    transport = transport or SmtpTransport()
    stats = {"sent": 0, "failed": 0, "errored_out": 0, "skipped": 0}

    if not transport.configured():
        await set_health(HEALTH_COMPONENT, "DEGRADED",
                         "SMTP not configured (mailer.env) — outbox accumulating")
        log.warning("mailer not configured; leaving outbox untouched")
        stats["skipped"] = 1
        return stats

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT message_id, subject, body, attempts FROM journal.outbox
               WHERE status='QUEUED' ORDER BY created_ts LIMIT %s""",
            (BATCH_LIMIT,))
        rows = await cur.fetchall()

    for message_id, subject, body, attempts in rows:
        try:
            await asyncio.to_thread(transport.send, subject, body)
        except Exception as e:                     # smtplib raises many types
            attempts += 1
            status = "FAILED" if attempts >= MAX_ATTEMPTS else "QUEUED"
            async with pool.connection() as conn:
                await conn.execute(
                    """UPDATE journal.outbox
                       SET attempts=%s, last_error=%s, status=%s
                       WHERE message_id=%s""",
                    (attempts, repr(e)[:300], status, message_id))
            stats["failed"] += 1
            if status == "FAILED":
                stats["errored_out"] += 1
                log.error("outbox row errored out", extra=kv(
                    message_id=message_id, attempts=attempts,
                    error=repr(e)[:200]))
            else:
                log.warning("send failed, will retry", extra=kv(
                    message_id=message_id, attempts=attempts,
                    error=repr(e)[:200]))
            continue
        async with pool.connection() as conn:
            await conn.execute(
                """UPDATE journal.outbox
                   SET status='SENT', sent_ts=now(), attempts=%s
                   WHERE message_id=%s""", (attempts + 1, message_id))
        stats["sent"] += 1
        log.info("sent", extra=kv(message_id=message_id, subject=subject[:80]))

    if stats["errored_out"]:
        await set_health(HEALTH_COMPONENT, "DEGRADED",
                         f"{stats['errored_out']} outbox rows errored out")
    else:
        await set_health(HEALTH_COMPONENT, "OK",
                         f"pass done: {stats['sent']} sent")
    return stats


async def main() -> None:
    from common.db import close_pool
    try:
        stats = await process_outbox()
        log.info("mailer pass", extra=kv(**stats))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
