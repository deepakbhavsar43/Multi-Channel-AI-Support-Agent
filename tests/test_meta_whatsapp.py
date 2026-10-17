from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.channels import meta_whatsapp, whatsapp
from app.config import get_settings
from tests.fake_llm import ScriptedLLM, ai, call

SECRET = "app-secret"
META_ENV = {"META_ACCESS_TOKEN": "token", "META_PHONE_NUMBER_ID": "1234567890",
            "META_APP_SECRET": SECRET, "META_VERIFY_TOKEN": "my-verify-token"}


def payload(text="hi", mid="wamid.ABC", sender="919800000001", kind="text"):
    msg = {"from": sender, "id": mid, "timestamp": "1760000000", "type": kind}
    if kind == "text":
        msg["text"] = {"body": text}
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550000000", "phone_number_id": "1234567890"},
            "contacts": [{"profile": {"name": "Riya"}, "wa_id": sender}],
            "messages": [msg]}}]}]}


def signed(body: bytes, secret: str = SECRET) -> dict:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


@pytest.fixture
def meta_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "cp.db"))
    for k, v in META_ENV.items():
        monkeypatch.setenv(k, v)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(meta_whatsapp, "send", lambda to, text: sent.append((to, text)) or True)
    monkeypatch.setattr(main.wa, "send", lambda to, text: pytest.fail("Twilio must not be used"))
    hello = ai(call("send_reply", message="Hi! How can I help?", kind="smalltalk", confidence="high"))
    monkeypatch.setattr(main, "make_llm", lambda s: ScriptedLLM(script=[hello]))
    get_settings.cache_clear()
    return TestClient(main.app), sent


def test_verification_handshake(meta_client):
    client, _ = meta_client
    with client:
        ok = client.get("/webhooks/meta/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": "my-verify-token", "hub.challenge": "12345"})
        assert ok.status_code == 200 and ok.text == "12345"
        bad = client.get("/webhooks/meta/whatsapp", params={
            "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "12345"})
        assert bad.status_code == 403


def test_signed_message_is_answered_once(meta_client):
    client, sent = meta_client
    body = json.dumps(payload()).encode()
    with client:
        assert client.get("/health").json()["whatsapp_provider"] == "meta"
        r = client.post("/webhooks/meta/whatsapp", content=body, headers=signed(body))
        assert r.status_code == 200
        # Meta retries deliver the same wamid again: no second reply
        client.post("/webhooks/meta/whatsapp", content=body, headers=signed(body))
    assert sent == [("whatsapp:+919800000001", "Hi! How can I help?")]


def test_bad_or_missing_signature_is_rejected(meta_client):
    client, sent = meta_client
    body = json.dumps(payload()).encode()
    with client:
        assert client.post("/webhooks/meta/whatsapp", content=body,
                           headers=signed(body, "other-secret")).status_code == 403
        assert client.post("/webhooks/meta/whatsapp", content=body,
                           headers={"Content-Type": "application/json"}).status_code == 403
    assert sent == []


def test_status_updates_are_acknowledged_and_ignored(meta_client):
    client, sent = meta_client
    status = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages",
              "value": {"statuses": [{"id": "wamid.X", "status": "delivered"}]}}]}]}
    body = json.dumps(status).encode()
    with client:
        assert client.post("/webhooks/meta/whatsapp", content=body, headers=signed(body)).status_code == 200
    assert sent == []


def test_parse_handles_media_and_buttons():
    image = meta_whatsapp.parse(payload(kind="image"))
    assert image[0].text == "" and image[0].user_id == "whatsapp:+919800000001"
    p = payload(kind="interactive")
    p["entry"][0]["changes"][0]["value"]["messages"][0]["interactive"] = {
        "type": "button_reply", "button_reply": {"id": "b1", "title": "Book a cleaning"}}
    assert meta_whatsapp.parse(p)[0].text == "Book a cleaning"
    assert meta_whatsapp.parse({"object": "page"}) == []


def test_send_builds_graph_request(monkeypatch):
    for k, v in META_ENV.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(meta_whatsapp.httpx, "post", fake_post)
    assert meta_whatsapp.send("whatsapp:+919800000001", "Hello") is True
    url, kw = calls[0]
    assert url == "https://graph.facebook.com/v23.0/1234567890/messages"
    assert kw["headers"]["Authorization"] == "Bearer token"
    assert kw["json"]["to"] == "919800000001" and kw["json"]["text"]["body"] == "Hello"


def test_send_failure_is_logged_not_raised(monkeypatch):
    for k, v in META_ENV.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(meta_whatsapp.httpx, "post", lambda url, **kw: httpx.Response(
        400, json={"error": {"code": 131030, "message": "Recipient not in allowed list"}},
        request=httpx.Request("POST", url)))
    assert meta_whatsapp.send("whatsapp:+919800000001", "Hello") is False
    from app import db
    assert db.rows("SELECT type FROM events WHERE type='send_failed'")


def test_provider_selection(monkeypatch):
    for k in ("META_ACCESS_TOKEN", "META_PHONE_NUMBER_ID", "TWILIO_ACCOUNT_SID",
              "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "WHATSAPP_PROVIDER"):
        monkeypatch.setenv(k, "")
    get_settings.cache_clear()
    assert whatsapp.provider() is None
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "t")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+1555")
    get_settings.cache_clear()
    assert whatsapp.provider() == "twilio"
    monkeypatch.setenv("META_ACCESS_TOKEN", "x")
    monkeypatch.setenv("META_PHONE_NUMBER_ID", "1")
    get_settings.cache_clear()
    assert whatsapp.provider() == "meta"          # auto prefers Meta
    monkeypatch.setenv("WHATSAPP_PROVIDER", "twilio")
    get_settings.cache_clear()
    assert whatsapp.provider() == "twilio"        # explicit choice wins


def test_twilio_public_url_without_scheme(monkeypatch):
    from starlette.requests import Request
    from app.channels import twilio_whatsapp
    monkeypatch.setenv("PUBLIC_BASE_URL", "blast-backdrop-paving.ngrok-free.dev")
    get_settings.cache_clear()
    req = Request({"type": "http", "method": "POST", "path": "/webhooks/twilio/whatsapp",
                   "query_string": b"", "headers": [], "scheme": "http",
                   "server": ("localhost", 8000)})
    assert twilio_whatsapp.public_url(req) == \
        "https://blast-backdrop-paving.ngrok-free.dev/webhooks/twilio/whatsapp"
