"""Mock clinic backend (appointments, tickets, lab orders) on SQLite.

Swap this module for real API clients (practice-management system, helpdesk)
without touching the agent: the tool layer only calls these functions.
Every function validates its inputs and raises BackendError with a message that
is safe to show to the model.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import db
from app.config import get_settings


class BackendError(Exception):
    """Expected business-rule failure; message is shown to the LLM."""


# Fees mirror data/docs/02_services_and_pricing.md and 03_appointment_policy.md.
# Tools return the fee for the service actually booked, so the model never has
# to remember (or carry over) a price from earlier in the conversation.
SERVICES: dict[str, dict] = {
    "consultation":   {"label": "New patient consultation", "dentist": "Dr. Asha Rao", "fee": "₹500"},
    "checkup":        {"label": "Routine check-up", "dentist": "Dr. Asha Rao", "fee": "₹300"},
    "cleaning":       {"label": "Scaling and polishing", "dentist": "Dr. Asha Rao", "fee": "₹1,500"},
    "filling":        {"label": "Filling", "dentist": "Dr. Asha Rao",
                       "fee": "₹1,800–₹3,000 per tooth, confirmed by the dentist"},
    "root_canal":     {"label": "Root canal treatment", "dentist": "Dr. Asha Rao",
                       "fee": "₹5,500–₹9,000, confirmed by the dentist"},
    "crown":          {"label": "Crown fitting", "dentist": "Dr. Asha Rao",
                       "fee": "₹6,000–₹12,000 per tooth, depending on the crown"},
    "whitening":      {"label": "Teeth whitening", "dentist": "Dr. Asha Rao", "fee": "₹8,000 (in-clinic)"},
    "orthodontic_consultation": {"label": "Orthodontic consultation", "dentist": "Dr. Kabir Mehta",
                                 "fee": "₹800"},
    "child_checkup":  {"label": "Child check-up", "dentist": "Dr. Neha Iyer", "fee": "₹400"},
    "emergency":      {"label": "Emergency visit", "dentist": "Dr. Asha Rao",
                       "fee": "₹700 emergency fee plus any treatment cost"},
}

# Policy (03_appointment_policy.md): two emergency slots every working day.
# Emergency visits are same-day only; a visit on a later day is a regular
# appointment and is charged as one (no emergency fee).
EMERGENCY_SLOTS_PER_DAY = 2
EMERGENCY_LATER_DAY_MSG = (
    "Emergency visits are same-day only. For {when}, book a regular appointment instead, "
    "usually service='consultation' (new patient, ₹500) or 'checkup' (existing patient, ₹300). "
    "The ₹700 emergency fee does NOT apply to a regular appointment. If symptoms are severe "
    "(heavy bleeding, swelling spreading to the eye or neck, trouble breathing or swallowing), "
    "the patient should go to a hospital emergency department now."
)

SLOT_MINUTES = 30
BOOKING_HORIZON_DAYS = 60
# (open, close) per weekday; Mon=0. Last slot starts 30 min before close.
HOURS = {d: (time(9), time(19)) for d in range(5)} | {5: (time(9), time(14))}
LUNCH = (time(13), time(14))  # weekdays only


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().clinic_timezone)


def now() -> datetime:
    """Clinic-local current time. Patched in tests."""
    return datetime.now(tz())


def _ref(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def _service(service: str) -> dict:
    if service not in SERVICES:
        raise BackendError(f"Unknown service '{service}'. Valid: {', '.join(SERVICES)}")
    return SERVICES[service]


def _day_slots(day: date) -> list[datetime]:
    if day.weekday() not in HOURS:
        return []
    open_t, close_t = HOURS[day.weekday()]
    cursor = datetime.combine(day, open_t, tz())
    last_start = datetime.combine(day, close_t, tz()) - timedelta(minutes=SLOT_MINUTES)
    slots = []
    while cursor <= last_start:
        in_lunch = day.weekday() < 5 and LUNCH[0] <= cursor.time() < LUNCH[1]
        if not in_lunch:
            slots.append(cursor)
        cursor += timedelta(minutes=SLOT_MINUTES)
    return slots


def _booked(dentist: str, day: date) -> set[str]:
    found = db.rows(
        "SELECT start_at FROM appointments WHERE dentist=? AND status='booked' AND start_at LIKE ?",
        (dentist, f"{day.isoformat()}%"),
    )
    return {r["start_at"] for r in found}


def _fmt(dt: datetime) -> str:
    return dt.strftime("%a %d %b %Y, %I:%M %p")


def _emergencies_booked(day: date) -> int:
    found = db.row(
        "SELECT COUNT(*) n FROM appointments WHERE service='emergency' AND status='booked'"
        " AND start_at LIKE ?", (f"{day.isoformat()}%",))
    return found["n"] if found else 0


def _next_working_day(after: date) -> date:
    day = after + timedelta(days=1)
    while day.weekday() not in HOURS:
        day += timedelta(days=1)
    return day


def check_availability(service: str, day: str) -> dict:
    info = _service(service)
    try:
        target = date.fromisoformat(day)
    except ValueError as exc:
        raise BackendError("date must be YYYY-MM-DD") from exc
    today = now().date()
    if target < today:
        raise BackendError("That date is in the past.")
    if target > today + timedelta(days=BOOKING_HORIZON_DAYS):
        raise BackendError(f"We only book up to {BOOKING_HORIZON_DAYS} days ahead.")
    if service == "emergency" and target != today:
        return {"date": day, "service": info["label"], "available": [],
                "note": EMERGENCY_LATER_DAY_MSG.format(when=target.strftime("%a %d %b"))}
    if target.weekday() not in HOURS:
        return {"date": day, "dentist": info["dentist"], "available": [],
                "note": "The clinic is closed on Sundays."}
    booked = _booked(info["dentist"], target)
    free = [s for s in _day_slots(target) if s.isoformat() not in booked and s > now()]
    result = {"date": day, "service": info["label"], "dentist": info["dentist"], "fee": info["fee"],
              "available": [s.strftime("%H:%M") for s in free]}
    if service == "emergency":
        left = EMERGENCY_SLOTS_PER_DAY - _emergencies_booked(target)
        if left <= 0 or not free:
            nxt = _next_working_day(today)
            result["available"] = []
            result["note"] = (
                "No emergency slots left today (the clinic is closed now or both daily emergency "
                "slots are taken). If symptoms are severe, the patient should go to a hospital "
                f"emergency department now. Otherwise offer a regular appointment on "
                f"{nxt.strftime('%a %d %b')} (e.g. service='consultation', ₹500); the emergency fee "
                "does not apply to it.")
    return result


def book_appointment(user_id: str, patient_name: str, service: str, start_time: str,
                     notes: str | None = None) -> dict:
    info = _service(service)
    name = (patient_name or "").strip()
    if len(name) < 2 or len(name) > 80:
        raise BackendError("A valid patient name is required.")
    try:
        start = datetime.fromisoformat(start_time)
    except ValueError as exc:
        raise BackendError("start_time must be ISO format YYYY-MM-DDTHH:MM") from exc
    start = start.replace(tzinfo=tz()) if start.tzinfo is None else start.astimezone(tz())
    start = start.replace(second=0, microsecond=0)

    if start <= now():
        raise BackendError("That time is in the past.")
    if start.date() > now().date() + timedelta(days=BOOKING_HORIZON_DAYS):
        raise BackendError(f"We only book up to {BOOKING_HORIZON_DAYS} days ahead.")
    if service == "emergency":
        if start.date() != now().date():
            raise BackendError(EMERGENCY_LATER_DAY_MSG.format(when=start.strftime("%a %d %b")))
        if _emergencies_booked(start.date()) >= EMERGENCY_SLOTS_PER_DAY:
            raise BackendError(
                "Both emergency slots for today are already taken. If symptoms are severe, the "
                "patient should go to a hospital emergency department; otherwise offer the earliest "
                "regular appointment (no emergency fee).")
    if start not in _day_slots(start.date()):
        alt = check_availability(service, start.date().isoformat())
        raise BackendError(
            f"{_fmt(start)} is outside clinic hours or not a slot boundary. "
            f"Free slots that day: {alt['available'][:6] or 'none'}"
        )

    ref = _ref("APT")
    try:
        db.execute(
            "INSERT INTO appointments(ref,user_id,patient_name,service,dentist,start_at,notes,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ref, user_id, name, service, info["dentist"], start.isoformat(),
             (notes or "")[:300], db.utcnow()),
        )
    except sqlite3.IntegrityError:
        alt = check_availability(service, start.date().isoformat())
        raise BackendError(
            f"That slot was just taken. Other free slots that day: {alt['available'][:6] or 'none'}"
        ) from None
    return {"booking_ref": ref, "patient_name": name, "service": info["label"],
            "dentist": info["dentist"], "when": _fmt(start), "fee": info["fee"]}


TICKET_CATEGORIES = {"billing", "insurance", "records", "complaint", "reschedule_cancel",
                     "handoff", "emergency", "other"}


def create_ticket(user_id: str, category: str, summary: str, priority: str = "normal",
                  thread_id: str | None = None) -> dict:
    if category not in TICKET_CATEGORIES:
        category = "other"
    if priority not in {"low", "normal", "high", "urgent"}:
        priority = "normal"
    summary = (summary or "").strip()
    if len(summary) < 5:
        raise BackendError("Ticket summary is too short; describe the request.")
    ref = _ref("TKT")
    db.execute(
        "INSERT INTO tickets(ref,user_id,thread_id,category,priority,summary,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (ref, user_id, thread_id, category, priority, summary[:2000], db.utcnow()),
    )
    return {"ticket_ref": ref, "category": category, "priority": priority,
            "sla": "Staff reply during working hours, usually within 1 working day."}


def get_order_status(user_id: str, order_id: str, last_name: str | None = None) -> dict:
    order_id = (order_id or "").strip().upper()
    order = db.row("SELECT * FROM lab_orders WHERE order_id=?", (order_id,))
    # Same response for "not found" and "not yours" so ids can't be probed.
    not_found = BackendError(
        "No order found for that ID and patient. Ask the user to double-check the order ID "
        "or confirm the patient's last name."
    )
    if not order:
        raise not_found
    owns = order["user_id"] == user_id
    name_ok = bool(last_name) and order["patient_name"].split()[-1].lower() == last_name.strip().lower()
    if not (owns or name_ok):
        if not last_name:
            return {"verification_required": True,
                    "message": "Ask the patient for their last name to verify this order."}
        raise not_found
    return {"order_id": order["order_id"], "item": order["item"], "status": order["status"],
            "expected_date": order["expected_date"]}


def seed_demo_data() -> None:
    """Idempotent demo lab orders. DEMO-ORD ids are referenced in the README."""
    today = now().date()
    demo = [
        ("ORD-1001", "web:demo", "Riya Sharma", "Zirconia crown (upper left molar)",
         "At lab — being milled", (today + timedelta(days=3)).isoformat()),
        ("ORD-1002", "web:demo", "Arjun Nair", "Clear aligners, sets 1-6",
         "Shipped from lab", (today + timedelta(days=1)).isoformat()),
        ("ORD-1003", "web:demo", "Meera Joshi", "Night guard",
         "Ready for collection at reception", today.isoformat()),
    ]
    for o in demo:
        db.execute(
            "INSERT OR IGNORE INTO lab_orders(order_id,user_id,patient_name,item,status,expected_date,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (*o, db.utcnow()),
        )
