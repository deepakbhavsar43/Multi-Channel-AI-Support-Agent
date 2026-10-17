"""Deterministic pre-LLM checks. Cheap, predictable, and easy to test.

These run before the model sees anything, so safety-critical routing (medical
emergencies, explicit requests for a person, card numbers) never depends on the
LLM making the right call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

EMERGENCY_PATTERNS = [
    r"can'?t breathe", r"cannot breathe", r"(difficulty|trouble|hard) (breathing|swallowing)",
    r"bleeding (won'?t|will not|doesn'?t|does not) stop", r"heavy bleeding", r"non[- ]?stop bleeding",
    r"swell\w* .{0,30}\b(eye|neck|throat)\b", r"\b(eye|neck|throat)\b.{0,30}swell",
    r"unconscious", r"passed out", r"fainted",
]
HUMAN_PATTERNS = [
    r"\b(talk|speak|chat)\w* (to|with) (a |an |the )?(human|person|someone|somebody|staff|agent|receptionist|doctor|dentist|manager|representative)\b",
    r"\b(real|actual|live) (person|human|agent)\b",
    r"\bhuman( agent)?\b(?! being)", r"^\s*(agent|operator|representative)\s*[.!?]*\s*$",
    r"\bcall me( back)?\b", r"\bconnect me\b",
]
CARD_PATTERN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
OTP_PATTERN = re.compile(r"\b(otp|cvv|pin)\b\D{0,15}\d{3,8}", re.I)

EMERGENCY_REPLY = (
    "This sounds like it may need urgent medical attention. If you have heavy bleeding that "
    "won't stop, difficulty breathing or swallowing, or swelling spreading to your eye or neck, "
    "please go to the nearest hospital emergency department now. I've also alerted our clinic "
    "team, who will contact you as soon as possible."
)
SENSITIVE_REPLY = (
    "For your safety, please don't share card numbers, CVV, PINs or OTPs here — our assistant "
    "never needs them. I've removed that message from our records. How else can I help?"
)


@dataclass
class GuardResult:
    action: Literal["pass", "emergency", "human", "sensitive", "reject"]
    reply: str | None = None
    clean_text: str = ""


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def redact(text: str) -> str:
    text = CARD_PATTERN.sub("[REDACTED-NUMBER]", text)
    return OTP_PATTERN.sub("[REDACTED-SECRET]", text)


def check(text: str, max_chars: int) -> GuardResult:
    clean = (text or "").strip()
    if not clean:
        return GuardResult("reject", "I can only read text messages for now. How can I help?", clean)
    if len(clean) > max_chars:
        return GuardResult("reject",
                           "That message is a bit long for me. Could you send a shorter summary?",
                           clean[:max_chars])
    if CARD_PATTERN.search(clean) or OTP_PATTERN.search(clean):
        return GuardResult("sensitive", SENSITIVE_REPLY, redact(clean))
    if _any(EMERGENCY_PATTERNS, clean):
        return GuardResult("emergency", EMERGENCY_REPLY, clean)
    if _any(HUMAN_PATTERNS, clean):
        return GuardResult("human", None, clean)
    return GuardResult("pass", None, clean)
