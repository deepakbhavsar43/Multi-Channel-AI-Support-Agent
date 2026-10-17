"""Meta WhatsApp Cloud API channel.

Inbound: Meta calls GET /webhooks/meta/whatsapp once to verify the endpoint
(hub.mode / hub.verify_token / hub.challenge), then POSTs JSON for every
incoming message and status update. Each POST is checked against the
X-Hub-Signature-256 header (HMAC-SHA256 of the raw body with the app secret),
acknowledged with 200 straight away, and processed in the background.
Meta retries non-200 responses for days, so message ids ("wamid...") are used
to drop duplicates.

Outbound: replies go to POST https://graph.facebook.com/<version>/<phone-number-id>/messages.
Free-form text is allowed within 24 hours of the user's last message.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app import db
from app.channels.twilio_whatsapp import split_message
from app.config import get_settings

logger = logging.getLogger(__name__)
TEXT_LIMIT = 4000  # WhatsApp allows 4096 characters per text message
GRAPH_URL = "https://graph.facebook.com"

# Graph API error codes worth explaining in the log.
ERROR_HINTS = {
    131030: "Recipient isn't in the test number's allowed list. Add it in App Dashboard > "
            "WhatsApp > API Setup > 'To'.",
    131047: "More than 24 hours since the user's last message; only templates are allowed now.",
    190: "Access token expired or invalid. Generate a new one (API Setup) or use a system user token.",
    100: "Invalid parameter. Check META_PHONE_NUMBER_ID (the ID, not the phone number).",
}


@dataclass
class Incoming:
    user_id: str      # "whatsapp:+<number>", same shape as the Twilio channel
    text: str
    message_id: str
    name: str | None


# ---------------------------------------------------------------- inbound
def verify_subscription(mode: str | None, token: str | None, challenge: str | None) -> str:
    """Answer Meta's one-time webhook verification. Returns the challenge or raises 403."""
    expected = get_settings().meta_verify_token
    if mode == "subscribe" and expected and token and hmac.compare_digest(token, expected):
        return challenge or ""
    logger.warning("meta webhook verification failed", extra={"mode": mode})
    raise HTTPException(status_code=403, detail="verification failed")


def verify_signature(raw_body: bytes, header: str | None) -> None:
    s = get_settings()
    if not s.meta_validate_signature:
        return
    if not s.meta_app_secret:
        logger.error("META_APP_SECRET is not set; rejecting webhook (set it, or "
                     "META_VALIDATE_SIGNATURE=false for local debugging only)")
        raise HTTPException(status_code=403, detail="signature check not configured")
    expected = "sha256=" + hmac.new(s.meta_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(header.strip(), expected):
        logger.warning("invalid meta signature")
        raise HTTPException(status_code=403, detail="invalid signature")


def _text_of(msg: dict) -> str:
    kind = msg.get("type")
    if kind == "text":
        return (msg.get("text") or {}).get("body", "")
    if kind == "button":
        return (msg.get("button") or {}).get("text", "")
    if kind == "interactive":
        inter = msg.get("interactive") or {}
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title", "")
    # image, audio, sticker, location ... -> empty: the guard asks for text
    return ""


def parse(payload: dict) -> list[Incoming]:
    """Extract user messages; status updates (sent/delivered/read) are ignored."""
    out: list[Incoming] = []
    if payload.get("object") != "whatsapp_business_account":
        return out
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                     for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                sender, mid = msg.get("from"), msg.get("id")
                if not sender or not mid:
                    continue
                out.append(Incoming(user_id=f"whatsapp:+{sender.lstrip('+')}",
                                    text=_text_of(msg), message_id=mid, name=names.get(sender)))
    return out


# ---------------------------------------------------------------- outbound
class MetaSendError(Exception):
    def __init__(self, status: int | None, code: int | None, message: str):
        super().__init__(f"HTTP {status} code {code}: {message}")
        self.status, self.code = status, code


def _transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    status = getattr(exc, "status", None)
    return status == 429 or (status is not None and status >= 500)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4),
       retry=retry_if_exception(_transient), reraise=True)
def _send_one(to_digits: str, body: str) -> str:
    s = get_settings()
    url = f"{GRAPH_URL}/{s.meta_graph_version}/{s.meta_phone_number_id}/messages"
    resp = httpx.post(
        url, timeout=15,
        headers={"Authorization": f"Bearer {s.meta_access_token}"},
        json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": to_digits,
              "type": "text", "text": {"preview_url": False, "body": body}},
    )
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        raise MetaSendError(resp.status_code, err.get("code"), err.get("message", resp.text[:200]))
    return (resp.json().get("messages") or [{}])[0].get("id", "")


def send(to: str, text: str) -> bool:
    """`to` is a user id like "whatsapp:+919800000000"."""
    s = get_settings()
    if not (s.meta_access_token and s.meta_phone_number_id):
        logger.error("meta whatsapp not configured", extra={"to": to})
        return False
    digits = to.removeprefix("whatsapp:").lstrip("+")
    try:
        for part in split_message(text, TEXT_LIMIT):
            _send_one(digits, part)
        return True
    except Exception as exc:
        code = getattr(exc, "code", None)
        logger.error("whatsapp send failed", exc_info=exc,
                     extra={"to": to, "provider": "meta", "hint": ERROR_HINTS.get(code)})
        db.log_event("send_failed", user_id=to, channel="whatsapp", provider="meta",
                     error=repr(exc)[:300])
        return False
