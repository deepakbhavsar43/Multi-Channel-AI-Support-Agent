from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import main
from app.config import get_settings
from tests.fake_llm import ScriptedLLM, ai, call

AUTH = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "cp.db"))
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(main.wa, "send", lambda to, text: sent.append((to, text)) or True)

    def make(script, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        monkeypatch.setattr(main, "make_llm", lambda s: ScriptedLLM(script=script))
        return TestClient(main.app), sent
    return make


def hello():
    return ai(call("send_reply", message="Hi! How can I help?", kind="smalltalk", confidence="high"))


def test_health_and_web_chat(client_factory):
    client, _ = client_factory([hello()])
    with client:
        assert client.get("/health").json()["retriever"] == "bm25"
        r = client.post("/api/chat", json={"session_id": "abcdef12", "message": "hi"})
        assert r.json()["reply"] == "Hi! How can I help?"
        assert client.post("/api/chat", json={"session_id": "../etc", "message": "x"}).status_code == 422


def test_whatsapp_webhook_signed_and_replied(client_factory):
    client, sent = client_factory([hello()], TWILIO_VALIDATE_SIGNATURE="true",
                                  TWILIO_AUTH_TOKEN="secret", PUBLIC_BASE_URL="https://bot.example.com")
    form = {"From": "whatsapp:+910000000001", "Body": "hi", "MessageSid": "SM123", "NumMedia": "0"}
    url = "https://bot.example.com/webhooks/twilio/whatsapp"
    sig = RequestValidator("secret").compute_signature(url, form)
    with client:
        bad = client.post("/webhooks/twilio/whatsapp", data=form, headers={"X-Twilio-Signature": "nope"})
        assert bad.status_code == 403
        ok = client.post("/webhooks/twilio/whatsapp", data=form, headers={"X-Twilio-Signature": sig})
        assert ok.status_code == 200 and "<Response>" in ok.text
        # Twilio retry of the same MessageSid must not produce a second reply
        client.post("/webhooks/twilio/whatsapp", data=form, headers={"X-Twilio-Signature": sig})
    assert sent == [("whatsapp:+910000000001", "Hi! How can I help?")]


def test_staff_console_flow(client_factory):
    client, sent = client_factory([hello()])
    with client:
        assert client.get("/admin/api/handoffs").status_code == 401
        form = {"From": "whatsapp:+910000000002", "Body": "talk to a human", "MessageSid": "SM9"}
        client.post("/webhooks/twilio/whatsapp", data=form)
        queue = client.get("/admin/api/handoffs", headers=AUTH).json()
        assert queue[0]["user_id"] == "whatsapp:+910000000002"
        assert queue[0]["ticket"]["status"] == "open"
        r = client.post("/admin/api/handoffs/whatsapp:+910000000002/reply", headers=AUTH,
                        json={"text": "Hi, Priya here."})
        assert r.json()["delivered"] is True
        client.post("/admin/api/handoffs/whatsapp:+910000000002/resolve", headers=AUTH)
        assert client.get("/admin/api/handoffs", headers=AUTH).json() == []
        stats = client.get("/admin/api/stats", headers=AUTH).json()
        assert {"reason": "user_requested", "n": 1} in stats["handoff_reasons"]
    assert sent[-2] == ("whatsapp:+910000000002", "Hi, Priya here.")
    # patient is told the assistant is back (it carries the conversation over)
    assert sent[-1][0] == "whatsapp:+910000000002" and "handed you back" in sent[-1][1]


def test_app_starts_without_llm_and_falls_back(client_factory, monkeypatch):
    client, _ = client_factory([])
    monkeypatch.setattr(main, "make_llm", lambda s: main.UnavailableLLM("no key"))
    with client:
        assert client.get("/health").json()["llm_ready"] is False
        r = client.post("/api/chat", json={"session_id": "abcdef99", "message": "hi"}).json()
        assert r["meta"]["kind"] == "fallback"


def test_ui_read_models(client_factory):
    """Endpoints behind the redesigned chat and console."""
    client, _ = client_factory([hello()])
    with client:
        status = client.get("/api/clinic").json()
        assert set(status) == {"staff_online", "label"}
        assert client.get("/api/chat/abcdef12/state").json() == {"status": "new", "ticket": None}
        r = client.post("/api/chat", json={"session_id": "abcdef12", "message": "talk to a human"}).json()
        assert r["status"] == "handoff" and r["ticket"].startswith("TKT-")
        assert r["meta"]["ticket"] == r["ticket"]
        assert client.get("/api/chat/abcdef12/state").json()["status"] == "handoff"
        assert client.get("/api/chat/..bad/state").status_code == 400

        assert client.get("/admin/api/cases/web:abcdef12").status_code == 401
        queue = client.get("/admin/api/handoffs", headers=AUTH).json()
        assert queue[0]["reason"] == "user_requested" and queue[0]["waiting_min"] == 0
        assert queue[0]["priority"] == "normal" and queue[0]["staff_joined"] is False

        case = client.get("/admin/api/cases/web:abcdef12", headers=AUTH).json()
        assert case["ticket"]["ref"] == r["ticket"] and case["reason"] == "user_requested"
        assert client.get("/admin/api/cases/web:nobody", headers=AUTH).status_code == 404

        today = client.get("/admin/api/stats", headers=AUTH).json()["today"]
        assert today["conversations"] == 1 and today["by_channel"] == {"web": 1}
        assert today["waiting"] == 1 and today["handled_by_assistant_pct"] == 0

        docs = client.get("/admin/api/docs?q=opening hours", headers=AUTH).json()
        assert docs and docs[0]["id"].startswith("01_clinic_info")
        assert len(client.get("/admin/api/docs", headers=AUTH).json()) > 10
