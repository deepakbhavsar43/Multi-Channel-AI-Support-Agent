"""Send a WhatsApp message through whichever provider is configured.

The agent, handoff and staff console only know "whatsapp:+<number>" user ids;
this module decides whether Meta Cloud API or Twilio delivers the text
(see WHATSAPP_PROVIDER in config).
"""
from __future__ import annotations

import logging

from app.channels import meta_whatsapp, twilio_whatsapp
from app.config import active_whatsapp_provider, get_settings

logger = logging.getLogger(__name__)


def provider() -> str | None:
    return active_whatsapp_provider(get_settings())


def send(to: str, text: str) -> bool:
    chosen = provider()
    if chosen == "meta":
        return meta_whatsapp.send(to, text)
    if chosen is None:
        logger.warning("no whatsapp provider fully configured; trying Twilio", extra={"to": to})
    # Twilio is the default; its send() logs and returns False if credentials are missing.
    return twilio_whatsapp.send(to, text)
