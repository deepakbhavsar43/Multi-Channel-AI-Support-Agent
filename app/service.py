"""Channel-agnostic conversation service.

Owns everything around the graph: sessions and memory threads, idempotency,
per-user serialisation, handoff state, failure counting and the fallback reply.
Channels (Twilio WhatsApp, web demo) call `handle_message` and deliver the reply.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage, HumanMessage

from app import db
from app.agent import guard as guard_mod
from app.agent import prompts
from app.config import get_settings
from app.tools import backend

logger = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str | None
    meta: dict = field(default_factory=dict)


class ConversationService:
    def __init__(self, graph):
        self.graph = graph
        self.settings = get_settings()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------ sessions
    def _lock(self, user_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks[user_id]

    def _new_thread(self, user_id: str) -> str:
        return f"{user_id}:{secrets.token_hex(4)}"

    def get_session(self, user_id: str, channel: str) -> dict:
        now = datetime.now(timezone.utc)
        s = db.row("SELECT * FROM sessions WHERE user_id=?", (user_id,))
        if s is None:
            thread = self._new_thread(user_id)
            db.execute("INSERT INTO sessions(user_id,channel,thread_id,started_at,last_seen_at)"
                       " VALUES (?,?,?,?,?)", (user_id, channel, thread, db.utcnow(), db.utcnow()))
            return db.row("SELECT * FROM sessions WHERE user_id=?", (user_id,))
        idle = now - datetime.fromisoformat(s["last_seen_at"])
        if s["status"] == "bot" and idle > timedelta(minutes=self.settings.session_timeout_min):
            # Session memory is per conversation: start a fresh thread after inactivity.
            s["thread_id"] = self._new_thread(user_id)
            s["failure_count"] = 0
            db.execute("UPDATE sessions SET thread_id=?, failure_count=0, started_at=? WHERE user_id=?",
                       (s["thread_id"], db.utcnow(), user_id))
            db.log_event("session_expired", s["thread_id"], user_id, idle_min=int(idle.total_seconds() // 60))
        db.execute("UPDATE sessions SET last_seen_at=? WHERE user_id=?", (db.utcnow(), user_id))
        return s

    def _set(self, user_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE sessions SET {cols} WHERE user_id=?", (*fields.values(), user_id))

    # ------------------------------------------------------------ main entry
    def handle_message(self, channel: str, user_id: str, text: str,
                       message_id: str | None = None) -> Reply | None:
        if message_id and not db.mark_processed(message_id):
            logger.info("duplicate message ignored", extra={"message_id": message_id})
            return None
        with self._lock(user_id):
            session = self.get_session(user_id, channel)
            timed_out = session["status"] == "handoff" and self._staff_went_idle(session)
            unanswered: list[str] = []
            if timed_out:
                unanswered = self.resolve_handoff(user_id, reason="staff_idle")
                session = db.row("SELECT * FROM sessions WHERE user_id=?", (user_id,))
            thread = session["thread_id"]
            db.log_message(thread, user_id, channel, "in", "user", guard_mod.redact(text or ""),
                           {"message_id": message_id} if message_id else None)
            if session["status"] == "handoff":
                reply = self._while_in_handoff(session, text)
            else:
                # messages the patient sent while staff were away are answered together
                reply = self._run_agent(session, "\n".join(unanswered + [text]))
            if timed_out and reply and reply.text:
                reply.text = f"{prompts.HANDOFF_TIMED_OUT_NOTE}\n\n{reply.text}"
            if reply and reply.text:
                db.log_message(thread, user_id, channel, "out", "bot", reply.text, reply.meta)
            return reply

    def _staff_went_idle(self, session: dict) -> bool:
        """Staff joined this handoff but haven't written for handoff_idle_timeout_min.
        Handoffs no one has picked up yet stay queued: the customer was promised a human."""
        last = db.row("SELECT MAX(created_at) AS at FROM messages WHERE thread_id=?"
                      " AND sender='human_agent'", (session["thread_id"],))
        if not last or not last["at"]:
            return False
        idle = datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])
        return idle > timedelta(minutes=self.settings.handoff_idle_timeout_min)

    def _while_in_handoff(self, session: dict, text: str) -> Reply | None:
        """A human owns the conversation: don't let the bot talk over them."""
        result = guard_mod.check(text, self.settings.max_input_chars)
        if result.action == "emergency":
            db.log_event("emergency_during_handoff", session["thread_id"], session["user_id"])
            return Reply(result.reply, {"kind": "guard"})
        # Once staff have joined, they own the replies; the thread is replaced on resolve.
        if db.row("SELECT 1 FROM messages WHERE thread_id=? AND sender='human_agent'",
                  (session["thread_id"],)):
            return None
        recent_ack = db.row(
            "SELECT 1 FROM messages WHERE thread_id=? AND sender='bot' AND meta LIKE '%in_handoff%'"
            " AND created_at > ?",
            (session["thread_id"],
             (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")),
        )
        if recent_ack:
            return None
        return Reply(prompts.IN_HANDOFF_ACK.format(ticket=session["handoff_ticket"]),
                     {"kind": "in_handoff"})

    def _run_agent(self, session: dict, text: str) -> Reply:
        user_id, thread = session["user_id"], session["thread_id"]
        config = {"configurable": {"thread_id": thread, "user_id": user_id},
                  "recursion_limit": 4 * self.settings.max_agent_steps + 10}
        try:
            state = self.graph.invoke({"user_input": text}, config)
        except Exception as exc:
            return self._on_failure(session, exc, text)

        self._set(user_id, failure_count=0)
        if state.get("handoff_ticket"):
            self._set(user_id, status="handoff", handoff_ticket=state["handoff_ticket"])
        reply = state.get("reply")
        if not reply:  # should not happen; never leave the user without an answer
            db.log_event("empty_reply", thread, user_id)
            return self._on_failure(session, RuntimeError("graph produced no reply"), text)
        return Reply(reply, state.get("reply_meta", {}))

    def _on_failure(self, session: dict, exc: Exception, text: str = "") -> Reply:
        user_id, thread = session["user_id"], session["thread_id"]
        failures = session["failure_count"] + 1
        self._set(user_id, failure_count=failures)
        logger.error("agent turn failed", exc_info=exc,
                     extra={"thread_id": thread, "failures": failures})
        db.log_event("error", thread, user_id, error=repr(exc)[:500], failures=failures)
        if failures >= self.settings.max_failures_before_handoff:
            ticket = backend.create_ticket(
                user_id, "handoff", f"Automatic handoff after {failures} consecutive system errors. "
                f"Last user message: {guard_mod.redact(text)[:300]}",
                "high", thread)["ticket_ref"]
            self._set(user_id, status="handoff", handoff_ticket=ticket)
            db.log_event("handoff", thread, user_id, reason="repeated_errors", ticket=ticket)
            return Reply(prompts.HANDOFF_REPLY.format(ticket=ticket),
                         {"kind": "handoff", "reason": "repeated_errors"})
        db.log_event("fallback", thread, user_id)
        return Reply(prompts.FALLBACK_REPLY, {"kind": "fallback"})

    # ------------------------------------------------------------ staff side
    def staff_reply(self, user_id: str, text: str, agent_name: str = "Staff") -> dict:
        s = db.row("SELECT * FROM sessions WHERE user_id=?", (user_id,))
        if not s:
            raise KeyError(user_id)
        db.log_message(s["thread_id"], user_id, s["channel"], "out", "human_agent", text,
                       {"agent": agent_name})
        return s

    def hand_back(self, user_id: str) -> list[str]:
        """Staff return the conversation to the bot. If the patient's last message was
        never answered (they wrote after staff's last reply), the bot answers it right
        away instead of waiting for them to write again. Returns the texts to deliver."""
        with self._lock(user_id):
            pending = self.resolve_handoff(user_id, reason="staff")
            s = db.row("SELECT * FROM sessions WHERE user_id=?", (user_id,))
            note = prompts.HANDED_BACK_NOTE if pending else (
                prompts.HANDED_BACK_NOTE + " " + prompts.HANDED_BACK_CONTINUE)
            db.log_message(s["thread_id"], user_id, s["channel"], "out", "system", note,
                           {"kind": "handed_back"})
            out = [note]
            if pending:
                reply = self._run_agent(s, "\n".join(pending))
                if reply and reply.text:
                    db.log_message(s["thread_id"], user_id, s["channel"], "out", "bot", reply.text,
                                   {**reply.meta, "proactive": True})
                    out.append(reply.text)
            return out

    def resolve_handoff(self, user_id: str, reason: str = "staff") -> list[str]:
        """Return the conversation to the bot with its memory carried over.
        Returns the patient's trailing unanswered messages (not yet in the bot's memory)."""
        s = db.row("SELECT * FROM sessions WHERE user_id=?", (user_id,))
        if not s:
            raise KeyError(user_id)
        if s["handoff_ticket"]:
            db.execute("UPDATE tickets SET status='resolved' WHERE ref=?", (s["handoff_ticket"],))
        new_thread = self._new_thread(user_id)
        pending = self._carry_over(s["thread_id"], new_thread, user_id)
        self._set(user_id, status="bot", handoff_ticket=None, failure_count=0, thread_id=new_thread)
        db.log_event("handoff_timeout" if reason == "staff_idle" else "handoff_resolved",
                     s["thread_id"], user_id, ticket=s["handoff_ticket"], reason=reason,
                     pending=len(pending))
        return pending

    def _carry_over(self, old_thread: str, new_thread: str, user_id: str,
                    max_messages: int = 20, max_chars: int = 4000) -> list[str]:
        """Give the assistant's fresh memory thread the conversation so far, including
        what the patient and staff said during the handoff (staff messages never pass
        through the graph), so the bot continues instead of starting over.

        Patient messages at the end that nobody answered are left out and returned, so
        the caller can feed them to the bot as the next turn."""
        rows = list(reversed(db.rows("SELECT sender, text, meta FROM messages WHERE thread_id=?"
                                     " ORDER BY id DESC LIMIT ?", (old_thread, max_messages))))
        pending: list[str] = []
        while rows and rows[-1]["sender"] == "user":
            pending.insert(0, rows.pop()["text"])
        if not rows:
            return pending
        lines = []
        for r in rows:
            if r["sender"] == "human_agent":
                agent = (json.loads(r["meta"]) if r["meta"] else {}).get("agent") or "Staff"
                who = f"Staff ({agent})"
            else:
                who = {"user": "Patient", "bot": "Assistant"}.get(r["sender"], "System")
            lines.append(f"{who}: {r['text']}")
        transcript = "\n".join(lines)[-max_chars:]
        config = {"configurable": {"thread_id": new_thread, "user_id": user_id}}
        try:
            # as_node="handoff" (which leads straight to END) so nothing is left pending;
            # the next invoke starts a normal turn from START with this history in place.
            self.graph.update_state(config, {"messages": [
                HumanMessage(prompts.HANDBACK_CONTEXT.format(transcript=transcript)),
                AIMessage(prompts.HANDBACK_ACK)]}, as_node="handoff")
        except Exception:  # never block a resolve on memory seeding
            logger.exception("could not carry conversation over to new thread",
                             extra={"thread_id": new_thread})
        return pending
