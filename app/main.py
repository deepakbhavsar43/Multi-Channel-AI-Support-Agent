"""FastAPI entrypoint: WhatsApp webhooks (Meta Cloud API, Twilio), web demo chat, staff console API."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app import console, db
from app.agent.graph import build_graph
from app.channels import meta_whatsapp, whatsapp
from app.channels import twilio_whatsapp as wa
from app.config import get_settings
from app.logging_setup import setup_logging
from app.rag.retriever import build_retriever
from app.service import ConversationService
from app.tools import backend

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class UnavailableLLM:
    """Stand-in when the model can't be initialised (e.g. missing API key):
    the app still starts, and every turn takes the fallback/handoff path."""

    def __init__(self, error: str):
        self.error = error

    def bind_tools(self, *a, **k):
        return self

    def invoke(self, *a, **k):
        raise RuntimeError(f"LLM unavailable: {self.error}")


def make_llm(settings):
    try:
        from langchain.chat_models import init_chat_model
        return init_chat_model(settings.llm_model, temperature=settings.llm_temperature,
                               timeout=settings.llm_timeout_s, max_retries=settings.llm_max_retries)
    except Exception as exc:
        logger.error("LLM init failed; running in fallback mode", extra={"error": repr(exc)})
        return UnavailableLLM(repr(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    db.init_db()
    backend.seed_demo_data()
    from langgraph.checkpoint.sqlite import SqliteSaver
    settings.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.checkpoint_db_path, check_same_thread=False)
    retriever = build_retriever(settings)
    llm = make_llm(settings)
    app.state.retriever = retriever
    app.state.llm_ok = not isinstance(llm, UnavailableLLM)
    app.state.service = ConversationService(build_graph(llm, retriever, SqliteSaver(conn)))
    logger.info("app started", extra={"retriever": retriever.kind, "llm": settings.llm_model,
                                      "llm_ok": app.state.llm_ok})
    yield
    conn.close()


app = FastAPI(title="Lotus Lane Dental — AI Support Agent", lifespan=lifespan)


def service(request: Request) -> ConversationService:
    return request.app.state.service


# ------------------------------------------------------------------ health
@app.get("/health")
def health(request: Request):
    s = get_settings()
    return {"status": "ok", "retriever": request.app.state.retriever.kind,
            "llm_model": s.llm_model, "llm_ready": request.app.state.llm_ok,
            "whatsapp_configured": whatsapp.provider() is not None,
            "whatsapp_provider": whatsapp.provider()}


@app.get("/")
def root():
    return RedirectResponse("/demo")


# ------------------------------------------------------------------ WhatsApp
def _process_whatsapp(svc: ConversationService, sender: str, text: str, sid: str) -> None:
    try:
        reply = svc.handle_message("whatsapp", sender, text, message_id=sid)
    except Exception as exc:  # last line of defence: the user still gets an answer
        logger.exception("unhandled error in whatsapp processing")
        db.log_event("error", user_id=sender, error=repr(exc)[:300], where="whatsapp_bg")
        from app.agent.prompts import FALLBACK_REPLY
        whatsapp.send(sender, FALLBACK_REPLY)
        return
    if reply and reply.text:
        whatsapp.send(sender, reply.text)


@app.post("/webhooks/twilio/whatsapp")
async def twilio_whatsapp(request: Request, background: BackgroundTasks):
    form = await wa.parse_and_verify(request)
    sender, sid = form.get("From", ""), form.get("MessageSid", "")
    text = form.get("Body", "")
    if int(form.get("NumMedia", "0") or 0) and not text.strip():
        text = ""  # guard replies that only text is supported
    if not sender.startswith("whatsapp:") or not sid:
        raise HTTPException(400, "not a WhatsApp message")
    # sync task -> Starlette runs it in the thread pool after the response is sent
    background.add_task(_process_whatsapp, service(request), sender, text, sid)
    return Response(wa.EMPTY_TWIML, media_type="application/xml")


@app.get("/webhooks/meta/whatsapp")
def meta_verify(request: Request):
    """One-time handshake when you save the Callback URL in the Meta App Dashboard."""
    q = request.query_params
    challenge = meta_whatsapp.verify_subscription(q.get("hub.mode"), q.get("hub.verify_token"),
                                                  q.get("hub.challenge"))
    return PlainTextResponse(challenge)


@app.post("/webhooks/meta/whatsapp")
async def meta_webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()
    meta_whatsapp.verify_signature(raw, request.headers.get("X-Hub-Signature-256"))
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "invalid JSON") from None
    for msg in meta_whatsapp.parse(payload):
        background.add_task(_process_whatsapp, service(request), msg.user_id, msg.text, msg.message_id)
    # Always 200 for valid, signed payloads (incl. status updates) so Meta doesn't retry.
    return {"status": "ok"}


# ------------------------------------------------------------------ web demo
class ChatIn(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{6,40}$")
    message: str = Field(max_length=4000)


_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, per_minute: int = 20) -> None:
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    q, now = _hits[ip], time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= per_minute:
        raise HTTPException(429, "Too many messages, slow down a little.")
    q.append(now)


@app.get("/demo")
def demo_page():
    return FileResponse(STATIC / "demo.html")


@app.post("/api/chat", dependencies=[Depends(rate_limit)])
async def chat(body: ChatIn, request: Request):
    reply = await run_in_threadpool(service(request).handle_message, "web",
                                    f"web:{body.session_id}", body.message)
    return {"reply": reply.text if reply else None, "meta": reply.meta if reply else {},
            **console.session_state(f"web:{body.session_id}")}


def _check_sid(session_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,40}", session_id):
        raise HTTPException(400, "bad session id")


@app.get("/api/clinic")
def clinic():
    return console.clinic_status()


@app.get("/api/chat/{session_id}/state")
def chat_state(session_id: str):
    _check_sid(session_id)
    return console.session_state(f"web:{session_id}")


@app.get("/api/chat/{session_id}/messages")
def chat_messages(session_id: str, after_id: int = 0):
    _check_sid(session_id)
    rows = db.rows("SELECT id, sender, text, meta, created_at FROM messages WHERE user_id=? AND id>?"
                   " ORDER BY id", (f"web:{session_id}", after_id))
    for r in rows:
        r["meta"] = json.loads(r["meta"]) if r["meta"] else {}
    return rows


# ------------------------------------------------------------------ staff console
def admin(authorization: str = Header(default="")) -> None:
    token = get_settings().admin_token
    if not token:
        raise HTTPException(403, "Admin API disabled: set ADMIN_TOKEN")
    if authorization != f"Bearer {token}":
        raise HTTPException(401, "bad token")


class StaffReply(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    agent_name: str = "Staff"


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/admin/api/handoffs", dependencies=[Depends(admin)])
def handoffs():
    return console.handoff_queue()


@app.get("/admin/api/cases/{user_id}", dependencies=[Depends(admin)])
def case_detail(user_id: str):
    found = console.case(user_id)
    if not found:
        raise HTTPException(404, "unknown user")
    return found


@app.get("/admin/api/docs", dependencies=[Depends(admin)])
def doc_snippets(request: Request, q: str | None = None, limit: int = 8):
    return console.doc_snippets(request.app.state.retriever, q, min(max(limit, 1), 50))


@app.get("/admin/api/conversations/{user_id}", dependencies=[Depends(admin)])
def conversation(user_id: str, limit: int = 50):
    return list(reversed(db.rows(
        "SELECT id, thread_id, direction, sender, text, meta, created_at FROM messages"
        " WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, min(limit, 500)))))


@app.post("/admin/api/handoffs/{user_id}/reply", dependencies=[Depends(admin)])
async def handoff_reply(user_id: str, body: StaffReply, request: Request):
    try:
        s = service(request).staff_reply(user_id, body.text, body.agent_name)
    except KeyError:
        raise HTTPException(404, "unknown user") from None
    delivered = True
    if s["channel"] == "whatsapp":
        delivered = await run_in_threadpool(whatsapp.send, user_id, body.text)
    return {"delivered": delivered}


@app.post("/admin/api/handoffs/{user_id}/resolve", dependencies=[Depends(admin)])
async def handoff_resolve(user_id: str, request: Request):
    try:
        texts = await run_in_threadpool(service(request).hand_back, user_id)
    except KeyError:
        raise HTTPException(404, "unknown user") from None
    if user_id.startswith("whatsapp:"):
        for text in texts:
            await run_in_threadpool(whatsapp.send, user_id, text)
    return {"status": "bot"}


@app.get("/admin/api/stats", dependencies=[Depends(admin)])
def stats():
    one = lambda sql, p=(): db.row(sql, p) or {}
    return {
        "conversations": one("SELECT COUNT(*) n FROM sessions")["n"],
        "messages_in": one("SELECT COUNT(*) n FROM messages WHERE direction='in'")["n"],
        "events": db.rows("SELECT type, COUNT(*) n FROM events GROUP BY type ORDER BY n DESC"),
        "handoff_reasons": db.rows(
            "SELECT json_extract(detail,'$.reason') reason, COUNT(*) n FROM events"
            " WHERE type='handoff' GROUP BY reason ORDER BY n DESC"),
        "tools": db.rows("SELECT name, COUNT(*) calls, SUM(1-ok) failures,"
                         " CAST(AVG(latency_ms) AS INT) avg_ms FROM tool_calls GROUP BY name"),
        "open_tickets": one("SELECT COUNT(*) n FROM tickets WHERE status='open'")["n"],
        "appointments": one("SELECT COUNT(*) n FROM appointments WHERE status='booked'")["n"],
        "today": console.today_stats(),
    }


@app.get("/admin/api/events", dependencies=[Depends(admin)])
def recent_events(type: str | None = None, limit: int = 100):
    if type:
        return db.rows("SELECT * FROM events WHERE type=? ORDER BY id DESC LIMIT ?", (type, min(limit, 500)))
    return db.rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (min(limit, 500),))


@app.get("/admin/api/tool-calls", dependencies=[Depends(admin)])
def recent_tool_calls(limit: int = 100):
    return db.rows("SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?", (min(limit, 500),))
