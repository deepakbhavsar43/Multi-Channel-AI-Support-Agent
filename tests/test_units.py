from __future__ import annotations

import pytest

from app.agent import guard
from app.config import get_settings
from app.rag.retriever import load_chunks
from app.tools import backend


@pytest.mark.parametrize("text,action", [
    ("How much is a cleaning?", "pass"),
    ("I want to speak to a human", "human"),
    ("agent", "human"),
    ("can someone call me back", "human"),
    ("Is my insurance agent's policy accepted?", "pass"),
    ("bleeding won't stop after extraction", "emergency"),
    ("I have difficulty breathing and my jaw hurts", "emergency"),
    ("my card 4111-1111-1111-1111", "sensitive"),
    ("OTP is 482913", "sensitive"),
    ("my number is +91 98765 43210", "pass"),
    ("", "reject"),
])
def test_guard(text, action):
    assert guard.check(text, 1500).action == action


def test_chunks_have_stable_unique_ids():
    chunks = load_chunks(get_settings().docs_dir)
    ids = [c.id for c in chunks]
    assert len(ids) == len(set(ids))
    assert "03_appointment_policy#no-shows" in ids


@pytest.mark.parametrize("query,expected", [
    ("how much does teeth cleaning cost", "02_services_and_pricing#cleaning-and-preventive-care"),
    ("late cancellation fee", "03_appointment_policy#rescheduling-and-cancellation"),
    ("do you do dental implants", "02_services_and_pricing#services-we-do-not-offer"),
    ("EMI for braces", "04_insurance_and_payments#emi-options"),
])
def test_retrieval_top_hit(retriever, query, expected):
    assert retriever.search(query, 4)[0].chunk.id == expected


def test_off_topic_query_is_below_threshold(retriever):
    hits = retriever.search("who won the cricket world cup", 4)
    assert not hits or hits[0].score < retriever.min_score


def test_slots_respect_hours_and_lunch():
    weekday = backend.check_availability("cleaning", "2026-10-15")["available"]
    assert weekday[0] == "09:00" and weekday[-1] == "18:30"
    assert "13:00" not in weekday and "13:30" not in weekday
    assert backend.check_availability("cleaning", "2026-10-17")["available"][-1] == "13:30"  # Sat
    assert backend.check_availability("cleaning", "2026-10-18")["available"] == []          # Sun


def test_double_booking_is_prevented():
    backend.book_appointment("u1", "Riya Sharma", "cleaning", "2026-10-15T11:00")
    with pytest.raises(backend.BackendError, match="just taken"):
        backend.book_appointment("u2", "Arjun Nair", "filling", "2026-10-15T11:00")  # same dentist
    # different dentist, same time is fine
    backend.book_appointment("u2", "Arjun Nair", "child_checkup", "2026-10-15T11:00")


@pytest.mark.parametrize("start", ["2026-10-15T13:00", "2026-10-15T11:10", "2026-10-18T10:00",
                                   "2026-10-13T10:00", "2027-03-01T10:00"])
def test_invalid_booking_times(start):
    with pytest.raises(backend.BackendError):
        backend.book_appointment("u1", "Riya Sharma", "cleaning", start)


# --- emergency booking rules (same-day only, 2 per day, correct fee) ---------

def test_emergency_is_same_day_only():
    later = backend.check_availability("emergency", "2026-10-15")
    assert later["available"] == [] and "same-day only" in later["note"]
    assert "does NOT apply" in later["note"]
    with pytest.raises(backend.BackendError, match="same-day only"):
        backend.book_appointment("u1", "Riya Sharma", "emergency", "2026-10-15T10:00")


def test_emergency_today_is_bookable_with_emergency_fee():
    today = backend.check_availability("emergency", "2026-10-14")
    assert today["available"] and "₹700" in today["fee"]
    booked = backend.book_appointment("u1", "Riya Sharma", "emergency", "2026-10-14T11:00")
    assert "₹700" in booked["fee"]


def test_only_two_emergency_slots_per_day():
    backend.book_appointment("u1", "Riya Sharma", "emergency", "2026-10-14T11:00")
    backend.book_appointment("u2", "Arjun Nair", "emergency", "2026-10-14T11:30")
    assert backend.check_availability("emergency", "2026-10-14")["available"] == []
    with pytest.raises(backend.BackendError, match="Both emergency slots"):
        backend.book_appointment("u3", "Meera Joshi", "emergency", "2026-10-14T12:00")


def test_emergency_after_closing_offers_next_working_day(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    # Saturday 17 Oct 2026, 15:00: clinic closed (Sat closes 14:00); Sunday closed too.
    monkeypatch.setattr(backend, "now", lambda: datetime(2026, 10, 17, 15, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
    res = backend.check_availability("emergency", "2026-10-17")
    assert res["available"] == [] and "Mon 19 Oct" in res["note"] and "hospital" in res["note"]


def test_regular_booking_returns_its_own_fee():
    booked = backend.book_appointment("u1", "Riya Sharma", "consultation", "2026-10-15T10:00")
    assert booked["fee"] == "₹500"
