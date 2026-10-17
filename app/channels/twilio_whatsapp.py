"""Twilio WhatsApp channel.

Inbound: Twilio POSTs a form to /webhooks/twilio/whatsapp. We verify the
X-Twilio-Signature, ACK immediately with empty TwiML (Twilio times out after
15s and LLM turns can be slower), then process in the background and send the
reply through the REST API. MessageSid is used to drop Twilio's retries.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app import db
from app.config import get_settings

logger = logging.getLogger(__name__)
WHATSAPP_LIMIT = 1500  # Twilio's hard limit is 1600 chars per message
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

_client = None


def client():
    global _client
    if _client is None:
        from twilio.rest import Client
        s = get_settings()
        if not (s.twilio_account_sid and s.twilio_auth_token):
            raise RuntimeError("Twilio credentials are not configured")
        _client = Client(s.twilio_account_sid, s.twilio_auth_token)
    return _client


def public_url(request: Request) -> str:
    """The exact URL Twilio called (needed for signature validation behind a proxy)."""
    base = get_settings().public_base_url.strip().rstrip("/")
    if base and "://" not in base:
        base = "https://" + base  # common .env mistake: host without scheme
    if base:
        url = base + request.url.path
        return url + (f"?{request.url.query}" if request.url.query else "")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return str(request.url.replace(scheme=proto, netloc=host))


async def parse_and_verify(request: Request) -> dict:
    form = dict((await request.form()).items())
    s = get_settings()
    if s.twilio_validate_signature:
        from twilio.request_validator import RequestValidator
        signature = request.headers.get("X-Twilio-Signature", "")
        if not s.twilio_auth_token or not RequestValidator(s.twilio_auth_token).validate(
                public_url(request), form, signature):
            logger.warning("invalid twilio signature", extra={"url": public_url(request)})
            raise HTTPException(status_code=403, detail="invalid signature")
    return form


def split_message(text: str, limit: int = WHATSAPP_LIMIT) -> list[str]:
    parts, current = [], ""
    for para in text.split("\n"):
        candidate = f"{current}\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(para) > limit:
            parts.append(para[:limit])
            para = para[limit:]
        current = para
    if current:
        parts.append(current)
    return parts or [""]


def _transient(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    return status is None or status == 429 or status >= 500


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, max=4),
       retry=retry_if_exception(_transient), reraise=True)
def _send_one(to: str, body: str) -> str:
    msg = client().messages.create(from_=get_settings().twilio_whatsapp_from, to=to, body=body)
    return msg.sid


def send(to: str, text: str) -> bool:
    try:
        for part in split_message(text):
            _send_one(to, part)
        return True
    except Exception as exc:
        hint = None
        if getattr(exc, "code", None) == 21654:  # "ContentSid Required"
            hint = ("Twilio trial accounts can only send approved templates. Upgrade Twilio, "
                    "or use the Meta WhatsApp Cloud API (see docs/SETUP.md).")
        logger.error("whatsapp send failed", exc_info=exc, extra={"to": to, "hint": hint})
        db.log_event("send_failed", user_id=to, channel="whatsapp", error=repr(exc)[:300])
        return False
