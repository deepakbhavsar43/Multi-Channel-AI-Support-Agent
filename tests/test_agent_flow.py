from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import db
from app.agent import prompts
from tests.fake_llm import ScriptedLLM, ai, call, last_search_ids, last_tool_result

USER = "web:demo"


def reply_with_sources(message="Cleaning costs ₹1,500.", kind="answer", confidence="high", n=1):
    return lambda msgs: ai(call("send_reply", message=message, kind=kind,
                                source_ids=last_search_ids(msgs)[:n], confidence=confidence))


def events(type_):
    return db.rows("SELECT * FROM events WHERE type=?", (type_,))


def session():
    return db.row("SELECT * FROM sessions WHERE user_id=?", (USER,))


def test_grounded_answer_is_delivered(make_service):
    llm = ScriptedLLM(script=[ai(call("search_clinic_docs", query="teeth cleaning price")),
                              reply_with_sources()])
    r = make_service(llm).handle_message("web", USER, "How much is a cleaning?")
    assert r.text == "Cleaning costs ₹1,500."
    assert r.meta["kind"] == "answer"
    assert r.meta["sources"][0].startswith("02_services_and_pricing")
    assert r.meta["source_labels"][r.meta["sources"][0]].startswith("Services and Pricing ›")
    logged = db.rows("SELECT name, ok FROM tool_calls")
    assert logged == [{"name": "search_clinic_docs", "ok": 1}]


def test_uncited_answer_is_rejected_then_self_corrects(make_service):
    llm = ScriptedLLM(script=[
        ai(call("send_reply", message="We open at 8am.", kind="answer", source_ids=[], confidence="high")),
        ai(call("search_clinic_docs", query="opening hours")),
        reply_with_sources("We open at 9:00 AM Monday to Saturday."),
    ])
    r = make_service(llm).handle_message("web", USER, "When do you open?")
    assert r.text.startswith("We open at 9:00")
    assert len(events("grounding_reject")) == 1
    # the rejection was fed back to the model as a tool error
    assert any(isinstance(m, ToolMessage) and "NOT sent" in m.content for m in llm.seen[1])


def test_repeated_ungrounded_answer_hands_off(make_service):
    bad = ai(call("send_reply", message="Implants cost ₹30,000.", kind="answer",
                  source_ids=["made-up#id"], confidence="high"))
    llm = ScriptedLLM(script=[bad, ai(call("send_reply", message="Implants cost ₹30,000.",
                                           kind="answer", source_ids=["x#y"], confidence="high"))])
    r = make_service(llm).handle_message("web", USER, "Price of implants?")
    assert r.meta["kind"] == "handoff"
    assert "TKT-" in r.text
    assert session()["status"] == "handoff"
    assert db.row("SELECT category FROM tickets")["category"] == "handoff"


def test_low_confidence_hands_off(make_service):
    llm = ScriptedLLM(script=[ai(call("search_clinic_docs", query="parking for trucks")),
                              reply_with_sources("Not sure.", confidence="low")])
    r = make_service(llm).handle_message("web", USER, "Can I park a truck?")
    assert r.meta["kind"] == "handoff" and r.meta["reason"] == "low_confidence"
    assert r.meta["ticket"].startswith("TKT-")
    assert len(events("low_confidence")) == 1


def test_explicit_human_request_skips_llm(make_service):
    llm = ScriptedLLM(script=[])  # any LLM call would fail the test
    r = make_service(llm).handle_message("web", USER, "I want to talk to a real person please")
    assert r.meta["reason"] == "user_requested"
    assert session()["status"] == "handoff"


def test_emergency_is_routed_urgently(make_service):
    r = make_service(ScriptedLLM(script=[])).handle_message(
        "web", USER, "my cheek is swollen and the swelling is spreading to my eye")
    assert "emergency department" in r.text and "TKT-" in r.text
    t = db.row("SELECT * FROM tickets")
    assert (t["category"], t["priority"]) == ("emergency", "high")


def test_booking_across_turns_uses_memory(make_service):
    def offer(msgs):
        res = last_tool_result(msgs, "check_availability")
        assert "11:00" in res["available"]
        return ai(call("send_reply", message="11:00 is free tomorrow. What's the patient's name?",
                       kind="action_result", confidence="high"))

    def book(msgs):
        # turn 2 sees turn 1 (memory)
        assert any(isinstance(m, HumanMessage) and "cleaning" in m.content for m in msgs)
        return ai(call("book_appointment", patient_name="Riya Sharma", service="cleaning",
                       start_time="2026-10-15T11:00"))

    def confirm(msgs):
        ref = last_tool_result(msgs, "book_appointment")["booking_ref"]
        return ai(call("send_reply", message=f"Booked! Your reference is {ref}.",
                       kind="action_result", confidence="high"))

    llm = ScriptedLLM(script=[ai(call("check_availability", service="cleaning", date="2026-10-15")),
                              offer, book, confirm])
    svc = make_service(llm)
    svc.handle_message("web", USER, "Can I book a cleaning tomorrow at 11?")
    r = svc.handle_message("web", USER, "Riya Sharma")
    appt = db.row("SELECT * FROM appointments")
    assert appt["ref"] in r.text and appt["start_at"].startswith("2026-10-15T11:00")
    assert appt["user_id"] == USER  # identity injected by executor, not the LLM
    # the UI renders a booking card from the structured action result
    booked = [x for x in r.meta["actions"] if x["tool"] == "book_appointment"][0]["result"]
    assert booked["booking_ref"] == appt["ref"] and booked["dentist"] == "Dr. Asha Rao"


def test_hallucinated_reference_is_blocked(make_service):
    llm = ScriptedLLM(script=[
        ai(call("send_reply", message="Done, booking APT-ABC123 confirmed.", kind="clarify", confidence="high")),
        ai(call("send_reply", message="Done, booking APT-ABC123 confirmed.", kind="clarify", confidence="high")),
    ])
    r = make_service(llm).handle_message("web", USER, "book me in")
    assert r.meta["kind"] == "handoff"
    assert "APT-ABC123" in events("grounding_reject")[0]["detail"]


def test_tool_business_error_goes_back_to_model(make_service):
    def recover(msgs):
        err = last_tool_result(msgs, "book_appointment")
        assert "past" in err["error"]
        return ai(call("send_reply", message="That time has passed. Which other time suits you?",
                       kind="clarify", confidence="high"))
    llm = ScriptedLLM(script=[ai(call("book_appointment", patient_name="Riya Sharma",
                                      service="cleaning", start_time="2026-10-01T10:00")), recover])
    r = make_service(llm).handle_message("web", USER, "book cleaning 1 Oct 10am, Riya Sharma")
    assert "passed" in r.text
    assert db.row("SELECT ok, error FROM tool_calls") == {"ok": 0, "error": "business_rule"}


def test_order_status_requires_ownership_or_verification(make_service):
    def answer(msgs):
        res = last_tool_result(msgs, "get_order_status")
        return ai(call("send_reply", message=f"ORD-1001: {res.get('status', 'need last name')}",
                       kind="action_result", confidence="high"))
    llm = ScriptedLLM(script=[ai(call("get_order_status", order_id="ord-1001")), answer,
                              ai(call("get_order_status", order_id="ORD-1001")),
                              ai(call("send_reply", message="Please share the patient's last name.",
                                      kind="clarify", confidence="high"))])
    svc = make_service(llm)
    assert "milled" in svc.handle_message("web", USER, "status of ORD-1001?").text
    r = svc.handle_message("web", "web:stranger", "status of ORD-1001?")
    assert "last name" in r.text
    result = db.rows("SELECT result FROM tool_calls WHERE name='get_order_status'")[-1]["result"]
    assert "verification_required" in result and "milled" not in result


def test_llm_outage_falls_back_then_hands_off(make_service):
    llm = ScriptedLLM(script=[TimeoutError("llm timeout"), TimeoutError("llm timeout")])
    svc = make_service(llm)
    r1 = svc.handle_message("web", USER, "hi")
    assert r1.meta["kind"] == "fallback"
    r2 = svc.handle_message("web", USER, "hello?")
    assert r2.meta == {"kind": "handoff", "reason": "repeated_errors"}
    assert session()["status"] == "handoff"
    assert len(events("error")) == 2


def test_duplicate_webhook_delivery_is_ignored(make_service):
    llm = ScriptedLLM(script=[ai(call("send_reply", message="Hi! How can I help?",
                                      kind="smalltalk", confidence="high"))])
    svc = make_service(llm)
    assert svc.handle_message("whatsapp", "whatsapp:+910000000001", "hi", message_id="SM1").text
    assert svc.handle_message("whatsapp", "whatsapp:+910000000001", "hi", message_id="SM1") is None


def test_handoff_mode_acks_once_then_resolves(make_service):
    llm = ScriptedLLM(script=[ai(call("send_reply", message="Hello again!", kind="smalltalk",
                                      confidence="high"))])
    svc = make_service(llm)
    svc.handle_message("web", USER, "talk to a human")
    first = svc.handle_message("web", USER, "are you there?")
    assert first.meta["kind"] == "in_handoff"
    assert svc.handle_message("web", USER, "hello??") is None  # no spam while waiting
    svc.staff_reply(USER, "Hi, this is Priya from reception.")
    assert svc.handle_message("web", USER, "thanks") is None  # bot stays quiet once staff joined
    # "thanks" came after staff's last message, so the bot answers it on hand-back
    assert svc.hand_back(USER) == [prompts.HANDED_BACK_NOTE, "Hello again!"]
    assert session()["status"] == "bot"
    # the bot's fresh thread carries the conversation, including the staff exchange,
    # so it continues instead of starting over
    sent = "\n".join(str(m.content) for m in llm.seen[-1])
    assert "Staff (Staff): Hi, this is Priya from reception." in sent
    assert "Patient: talk to a human" in sent and "hello??" in sent


def test_bot_stays_quiet_once_staff_have_replied(make_service):
    svc = make_service(ScriptedLLM(script=[]))
    svc.handle_message("web", USER, "talk to a human")
    svc.staff_reply(USER, "Hi, Dr. Rao can help with implants.")
    assert svc.handle_message("web", USER, "thanks") is None  # no ack talking over staff


def test_bot_takes_back_handoff_when_staff_go_idle(make_service):
    llm = ScriptedLLM(script=[ai(call("send_reply", message="You're welcome!", kind="smalltalk",
                                      confidence="high"))])
    svc = make_service(llm)
    svc.handle_message("web", USER, "talk to a human")
    ticket = session()["handoff_ticket"]
    svc.staff_reply(USER, "Hi, Dr. Rao can help with implants.")
    db.execute("UPDATE messages SET created_at='2000-01-01T00:00:00+00:00' WHERE sender='human_agent'")
    r = svc.handle_message("web", USER, "thanks")
    assert r.text.startswith(prompts.HANDOFF_TIMED_OUT_NOTE) and r.text.endswith("You're welcome!")
    assert session()["status"] == "bot"
    assert db.row("SELECT status FROM tickets WHERE ref=?", (ticket,))["status"] == "resolved"
    assert db.row("SELECT 1 FROM events WHERE type='handoff_timeout'")


def test_unclaimed_handoff_does_not_time_out(make_service):
    svc = make_service(ScriptedLLM(script=[]))
    svc.handle_message("web", USER, "talk to a human")
    db.execute("UPDATE messages SET created_at='2000-01-01T00:00:00+00:00'")
    assert svc.handle_message("web", USER, "anyone?").meta["kind"] == "in_handoff"
    assert session()["status"] == "handoff"


def test_card_number_is_redacted_and_not_sent_to_llm(make_service):
    r = make_service(ScriptedLLM(script=[])).handle_message(
        "web", USER, "my card is 4111 1111 1111 1111 pls charge")
    assert "don't share card numbers" in r.text
    stored = db.rows("SELECT text FROM messages WHERE direction='in'")[0]["text"]
    assert "4111" not in stored


def test_history_stays_valid_for_provider_apis(make_service, retriever):
    """Every AI tool call must be followed by its ToolMessage, or OpenAI/Anthropic reject the next turn."""
    llm = ScriptedLLM(script=[ai(call("search_clinic_docs", query="sunday")),
                              reply_with_sources("We're closed on Sundays."),
                              ai(call("send_reply", message="You're welcome!", kind="smalltalk",
                                      confidence="high"))])
    svc = make_service(llm)
    svc.handle_message("web", USER, "open sunday?")
    svc.handle_message("web", USER, "thanks")
    msgs = llm.seen[-1][1:]
    pending = set()
    for m in msgs:
        if isinstance(m, AIMessage):
            assert not pending, f"unanswered tool calls {pending}"
            pending = {c["id"] for c in m.tool_calls}
        elif isinstance(m, ToolMessage):
            pending.discard(m.tool_call_id)
        elif isinstance(m, HumanMessage):
            assert not pending


def test_hand_back_answers_unanswered_message_right_away(make_service):
    """Patient wrote after staff's last reply; on hand-back the bot replies to it
    immediately, with the earlier conversation in memory."""
    llm = ScriptedLLM(script=[ai(call("send_reply", message="Happy to help you book. Which service and day?",
                                      kind="clarify", confidence="high"))])
    svc = make_service(llm)
    svc.handle_message("web", USER, "I'd like to talk to a person, please.")
    svc.staff_reply(USER, "hi", "Deepak")
    assert svc.handle_message("web", USER, "i want to book appointment") is None  # staff own it
    out = svc.hand_back(USER)
    assert out == [prompts.HANDED_BACK_NOTE, "Happy to help you book. Which service and day?"]
    seen = llm.seen[-1]
    assert seen[-1].content == "i want to book appointment"           # answered as this turn
    assert "Staff (Deepak): hi" in str(seen[1].content)                # with prior context
    assert "i want to book appointment" not in str(seen[1].content)    # not duplicated
    proactive = db.row("SELECT meta FROM messages WHERE sender='bot' ORDER BY id DESC LIMIT 1")
    assert '"proactive": true' in proactive["meta"]
    assert session()["status"] == "bot"


def test_emergency_fee_not_quoted_for_later_day_booking(make_service):
    """Regression: patient wanted an emergency visit, clinic couldn't take it, patient agreed to a
    later day. The later-day booking is a regular consultation and must not carry the ₹700 fee."""
    def wrong_fee(msgs):
        ref = last_tool_result(msgs, "book_appointment")["booking_ref"]
        return ai(call("send_reply", message=f"Booked {ref} for tomorrow 10:00. Emergency fee ₹700 applies.",
                       kind="action_result", confidence="high"))

    def right_fee(msgs):
        res = last_tool_result(msgs, "book_appointment")
        return ai(call("send_reply", message=f"Booked {res['booking_ref']} for tomorrow 10:00 "
                                             f"(consultation, {res['fee']}).",
                       kind="action_result", confidence="high"))

    llm = ScriptedLLM(script=[
        # 1. model first tries to keep the emergency service for tomorrow -> backend refuses
        ai(call("book_appointment", patient_name="Riya Sharma", service="emergency",
                start_time="2026-10-15T10:00")),
        # 2. it switches to a regular consultation, as the tool error tells it to
        ai(call("book_appointment", patient_name="Riya Sharma", service="consultation",
                start_time="2026-10-15T10:00")),
        # 3. but still quotes the emergency fee -> verify rejects, model corrects itself
        wrong_fee, right_fee,
    ])
    r = make_service(llm).handle_message("web", USER, "ok book me tomorrow 10am instead, Riya Sharma")
    assert "₹500" in r.text and "700" not in r.text
    appt = db.row("SELECT service FROM appointments")
    assert appt["service"] == "consultation"
    first_error = db.rows("SELECT result FROM tool_calls WHERE name='book_appointment'")[0]["result"]
    assert "same-day only" in first_error
    assert "emergency fee" in events("grounding_reject")[0]["detail"]


def test_price_from_tool_is_allowed_in_clarify(make_service):
    llm = ScriptedLLM(script=[
        ai(call("check_availability", service="consultation", date="2026-10-15")),
        lambda msgs: ai(call("send_reply", message="Tomorrow 10:00 is free for a consultation (₹500). "
                                                   "Shall I book it? What's the patient's name?",
                             kind="clarify", confidence="high")),
    ])
    r = make_service(llm).handle_message("web", USER, "consultation tomorrow?")
    assert "₹500" in r.text and not events("grounding_reject")
