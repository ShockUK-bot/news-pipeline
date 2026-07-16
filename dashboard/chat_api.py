"""C6 dashboard — CHAT tab backend (spec v1.3 §4b/§5).

Drop-in FastAPI router for the final-build (Postgres) dashboard. The demo
SQLite dashboard cannot host chat: chat needs the pipeline's Postgres queue
and a running a13-chat service. Integration into app.py:

    from chat_api import make_chat_router
    app.include_router(make_chat_router(require_basic_auth))

where `require_basic_auth` is the existing Basic-auth dependency. The kill
token for /api/chat/file is read from DASH_KILL_TOKEN, same as /api/kill.

Write surface (posture): the dashboard writes ONLY journal.chat_sessions,
journal.chat_messages, journal.audit, and enqueues chat.request. The pipeline
write (signal.synthetic) happens in A13 after its own code gates — the
dashboard still never commands the pipeline directly.
"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from common.db import get_pool, jb
from common.queue import enqueue

CHAT_QUEUE = "chat.request"
MAX_QUESTION_CHARS = 2000


class AskBody(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    session_id: int | None = None


class FileBody(BaseModel):
    message_id: int          # the ASSISTANT/ANSWER row carrying the proposal
    token: str               # DASH_KILL_TOKEN — third token-gated write action


def _check_kill_token(token: str) -> None:
    expected = os.environ.get("DASH_KILL_TOKEN", "")
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="bad token")


def make_chat_router(auth_dep) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth_dep)])

    @router.get("/api/chat/state")
    async def chat_state(session_id: int | None = None, after_id: int = 0):
        pool = await get_pool()
        async with pool.connection() as conn:
            if session_id is None:
                cur = await conn.execute(
                    """SELECT session_id FROM journal.chat_sessions
                       ORDER BY session_id DESC LIMIT 1""")
                row = await cur.fetchone()
                if row is None:
                    return {"session_id": None, "messages": []}
                session_id = row[0]
            cur = await conn.execute(
                """SELECT id, session_id, ts, role, kind, content, reply_to,
                          proposal, decision_id, status, latency_ms
                   FROM journal.dash_chat
                   WHERE session_id = %s AND id > %s
                   ORDER BY id LIMIT 200""",
                (session_id, after_id))
            cols = [d.name for d in cur.description]
            messages = [dict(zip(cols, r)) for r in await cur.fetchall()]
        return {"session_id": session_id, "messages": messages}

    @router.get("/api/chat/sessions")
    async def chat_sessions(limit: int = 20):
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT session_id, EXTRACT(EPOCH FROM created_ts) AS created_ts,
                          title
                   FROM journal.chat_sessions
                   ORDER BY session_id DESC LIMIT %s""", (min(limit, 100),))
            cols = [d.name for d in cur.description]
            return {"sessions": [dict(zip(cols, r)) for r in await cur.fetchall()]}

    @router.post("/api/chat/message")
    async def chat_message(body: AskBody):
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.transaction():
                session_id = body.session_id
                if session_id is None:
                    cur = await conn.execute(
                        """INSERT INTO journal.chat_sessions (title)
                           VALUES (%s) RETURNING session_id""",
                        (body.content[:80],))
                    session_id = (await cur.fetchone())[0]
                cur = await conn.execute(
                    """INSERT INTO journal.chat_messages
                       (session_id, role, kind, content, status)
                       VALUES (%s,'OPERATOR','ASK',%s,'PENDING')
                       RETURNING message_id""",
                    (session_id, body.content))
                message_id = (await cur.fetchone())[0]
                await enqueue(CHAT_QUEUE, f"chat-{message_id}",
                              {"msg_schema": "chat.request/1",
                               "body": {"message_id": message_id,
                                        "session_id": session_id,
                                        "kind": "ASK"}},
                              priority=200, conn=conn)
        return {"ok": True, "session_id": session_id, "message_id": message_id}

    @router.post("/api/chat/file")
    async def chat_file(body: FileBody):
        _check_kill_token(body.token)
        operator = os.environ.get("DASH_USER", "operator")
        pool = await get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                """SELECT session_id, proposal FROM journal.chat_messages
                   WHERE message_id = %s AND role = 'ASSISTANT'
                     AND proposal IS NOT NULL""",
                (body.message_id,))
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=400,
                                    detail="no filing proposal on that message")
            session_id, proposal = row
            # refuse double-filing the same proposal message
            cur = await conn.execute(
                """SELECT 1 FROM journal.chat_messages
                   WHERE kind = 'FILE_REQUEST'
                     AND (proposal->>'source_message_id')::bigint = %s LIMIT 1""",
                (body.message_id,))
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail="already filed")

            filing = {**proposal, "operator": operator,
                      "source_message_id": body.message_id}
            async with conn.transaction():
                cur = await conn.execute(
                    """INSERT INTO journal.chat_messages
                       (session_id, role, kind, content, reply_to, proposal, status)
                       VALUES (%s,'OPERATOR','FILE_REQUEST',%s,%s,%s,'PENDING')
                       RETURNING message_id""",
                    (session_id,
                     f"FILE {proposal.get('ticker')} for evaluation",
                     body.message_id, jb(filing)))
                file_msg_id = (await cur.fetchone())[0]
                await conn.execute(
                    """INSERT INTO journal.audit (actor, action, old_value, new_value, detail)
                       VALUES (%s, 'CHAT_FILE_REQUESTED', NULL, %s, %s)""",
                    (operator, proposal.get("ticker"),
                     f"chat message {body.message_id}"))
                await enqueue(CHAT_QUEUE, f"chat-{file_msg_id}",
                              {"msg_schema": "chat.request/1",
                               "body": {"message_id": file_msg_id,
                                        "session_id": session_id,
                                        "kind": "FILE"}},
                              priority=150, conn=conn)
        return {"ok": True, "message_id": file_msg_id}

    return router
