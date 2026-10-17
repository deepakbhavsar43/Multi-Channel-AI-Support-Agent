"""Read models for the web UIs: clinic status for the patient chat, and the
staff console's queue, case panel, daily stats and knowledge-base snippets.

Kept out of main.py so the HTTP layer stays thin and these queries are testable.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

from app import db
from app.tools import backend


# ------------------------------------------------------------------ helpers
def _today_start_utc() -> str:
    """Clinic-local midnight, as a UTC ISO string comparable with created_at columns."""
    local = backend.now()
    start = datetime.combine(local.date(), time(0), local.tzinfo)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def _minutes_since(iso: str | None) -> int | None:
    if not iso:
        return None
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    return max(0, int(delta.total_seconds() // 60))


def _detail(event: dict | None) -> dict:
    try:
        return json.loads(event["detail"]) if event and event.get("detail") else {}
    except (ValueError, TypeError):
        return {}


def _fmt_clock(t: time) -> str:
    return datetime.combine(datetime.today(), t).strftime("%I:%M %p").lstrip("0")


# ------------------------------------------------------------------ patient side
def clinic_status() -> dict:
    """Whether staff are around right now, from the same opening hours the booking tool uses."""
    now = backend.now()
    hours = backend.HOURS.get(now.weekday())
    in_lunch = now.weekday() < 5 and backend.LUNCH[0] <= now.time() < backend.LUNCH[1]
    if hours and hours[0] <= now.time() < hours[1] and not in_lunch:
        return {"staff_online": True, "label": f"Staff online until {_fmt_clock(hours[1])}"}
    if hours and in_lunch:
        return {"staff_online": False, "label": f"Staff back at {_fmt_clock(backend.LUNCH[1])}"}
    for ahead in range(0, 8):  # next opening
        day = now.date() + timedelta(days=ahead)
        h = backend.HOURS.get(day.weekday())
        if h and (ahead > 0 or now.time() < h[0]):
            when = "today" if ahead == 0 else ("tomorrow" if ahead == 1 else day.strftime("%A"))
            return {"staff_online": False, "label": f"Staff back {when} at {_fmt_clock(h[0])}"}
    return {"staff_online": False, "label": "Staff offline"}


def session_state(user_id: str) -> dict:
    s = db.row("SELECT status, handoff_ticket FROM sessions WHERE user_id=?", (user_id,))
    return {"status": s["status"], "ticket": s["handoff_ticket"]} if s else {"status": "new", "ticket": None}


# ------------------------------------------------------------------ staff side
def patient_name(user_id: str) -> str | None:
    appt = db.row("SELECT patient_name FROM appointments WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                  (user_id,))
    return appt["patient_name"] if appt else None


def _handoff_event(user_id: str) -> dict:
    return _detail(db.row("SELECT detail FROM events WHERE user_id=? AND type='handoff'"
                          " ORDER BY id DESC LIMIT 1", (user_id,)))


def priority_of(ticket: dict | None) -> str:
    if not ticket:
        return "normal"
    if ticket["category"] == "emergency" or ticket["priority"] == "urgent":
        return "urgent"
    return ticket["priority"]


def handoff_queue() -> list[dict]:
    out = []
    for s in db.rows("SELECT * FROM sessions WHERE status='handoff' ORDER BY last_seen_at DESC"):
        ticket = db.row("SELECT * FROM tickets WHERE ref=?", (s["handoff_ticket"],))
        staff_joined = db.row("SELECT 1 FROM messages WHERE thread_id=? AND sender='human_agent'",
                              (s["thread_id"],)) is not None
        out.append({**s, "ticket": ticket, "name": patient_name(s["user_id"]),
                    "reason": _handoff_event(s["user_id"]).get("reason"),
                    "priority": priority_of(ticket),
                    "waiting_min": _minutes_since(ticket["created_at"] if ticket else s["last_seen_at"]),
                    "staff_joined": staff_joined})
    rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    out.sort(key=lambda h: (rank.get(h["priority"], 2), -(h["waiting_min"] or 0)))
    return out


def case(user_id: str) -> dict | None:
    s = db.row("SELECT * FROM sessions WHERE user_id=?", (user_id,))
    if not s:
        return None
    ticket = db.row("SELECT * FROM tickets WHERE ref=?", (s["handoff_ticket"],)) if s["handoff_ticket"] else None
    upcoming = db.rows(
        "SELECT ref, service, dentist, start_at FROM appointments WHERE user_id=? AND status='booked'"
        " AND start_at >= ? ORDER BY start_at LIMIT 5", (user_id, backend.now().isoformat()))
    for a in upcoming:
        a["service_label"] = backend.SERVICES.get(a["service"], {}).get("label", a["service"])
    tools = db.rows(
        "SELECT t.id, t.name, t.ok, t.error, t.latency_ms, t.result, t.created_at FROM tool_calls t"
        " JOIN (SELECT DISTINCT thread_id FROM messages WHERE user_id=?) m ON m.thread_id=t.thread_id"
        " ORDER BY t.id DESC LIMIT 40", (user_id,))
    for t in tools:
        t["summary"] = _tool_summary(t)
        del t["result"]
    return {"user_id": user_id, "channel": s["channel"], "status": s["status"],
            "name": patient_name(user_id), "ticket": ticket, "priority": priority_of(ticket),
            "reason": _handoff_event(user_id).get("reason") if ticket else None,
            "upcoming": upcoming, "tool_calls": list(reversed(tools))}


def _tool_summary(t: dict) -> str:
    try:
        r = json.loads(t["result"] or "{}")
    except ValueError:
        r = {}
    if not t["ok"]:
        return (t["error"] or "failed")
    if isinstance(r, dict):
        for key in ("booking_ref", "ticket_ref", "order_id"):
            if key in r:
                return r[key]
        if "passages" in r:
            return f"{len(r['passages'])} passages"
        if "available" in r:
            return f"{len(r['available'])} free slots"
    return "ok"


def today_stats() -> dict:
    start = _today_start_utc()
    one = lambda sql, p=(): (db.row(sql, p) or {}).get("n") or 0  # noqa: E731
    by_channel = {r["channel"]: r["n"] for r in db.rows(
        "SELECT channel, COUNT(DISTINCT user_id) n FROM messages WHERE direction='in' AND created_at>=?"
        " GROUP BY channel", (start,))}
    conversations = sum(by_channel.values())
    handed = one("SELECT COUNT(DISTINCT user_id) n FROM events WHERE type='handoff' AND created_at>=?", (start,))
    rejects = one("SELECT COUNT(*) n FROM events WHERE type='grounding_reject' AND created_at>=?", (start,))
    failed_twice = one("SELECT COUNT(*) n FROM events WHERE type='handoff' AND created_at>=?"
                       " AND json_extract(detail,'$.reason') LIKE 'grounding_failed%'", (start,))
    queue = handoff_queue()
    return {
        "waiting": len(queue),
        "oldest_wait_min": max((q["waiting_min"] or 0 for q in queue), default=None),
        "conversations": conversations,
        "by_channel": by_channel,
        "handoffs": one("SELECT COUNT(*) n FROM events WHERE type='handoff' AND created_at>=?", (start,)),
        "handled_by_assistant_pct": round(100 * (conversations - handed) / conversations) if conversations else None,
        "bookings": one("SELECT COUNT(*) n FROM appointments WHERE created_at>=?", (start,)),
        "grounding_rejects": rejects,
        "self_corrected": max(0, rejects - 2 * failed_twice),
        "fallbacks": one("SELECT COUNT(*) n FROM events WHERE type='fallback' AND created_at>=?", (start,)),
        "errors": one("SELECT COUNT(*) n FROM events WHERE type='error' AND created_at>=?", (start,)),
        "tools": db.rows("SELECT name, COUNT(*) calls, SUM(1-ok) failures, CAST(AVG(latency_ms) AS INT) avg_ms"
                         " FROM tool_calls WHERE created_at>=? GROUP BY name ORDER BY calls DESC", (start,)),
    }


def doc_snippets(retriever, q: str | None = None, limit: int = 8) -> list[dict]:
    """Knowledge-base sections staff can paste into a reply."""
    if q and q.strip():
        hits = retriever.search(q.strip(), limit)
        chunks = [h.chunk for h in hits]
    else:
        chunks = list(getattr(retriever, "chunks_by_id", {}).values())
    return [{"id": c.id, "title": c.title, "section": c.section, "text": c.text} for c in chunks]
