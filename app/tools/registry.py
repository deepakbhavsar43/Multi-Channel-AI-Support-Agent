"""Tool schemas exposed to the LLM, and a single executor that runs them.

The executor (not the LLM) injects the caller's identity, so the model can never
book for, or read orders of, another user. Every call is timed and logged, and
any failure becomes a structured error the model can react to instead of an
exception that kills the turn.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, ValidationError

from app import db
from app.tools import backend

logger = logging.getLogger(__name__)

ServiceName = Literal[tuple(backend.SERVICES)]  # type: ignore[valid-type]


# --- schemas (docstrings become tool descriptions) ---------------------------

class search_clinic_docs(BaseModel):
    """Search the clinic's official documents (hours, prices, policies, insurance,
    aftercare, FAQ). ALWAYS call this before answering any factual question about
    the clinic. Returns passages with ids you must cite."""
    query: str = Field(description="Self-contained search query, e.g. 'late cancellation fee'")


class check_availability(BaseModel):
    """List free appointment slots for a service on one date."""
    service: ServiceName
    date: str = Field(description="YYYY-MM-DD in clinic local time")


class book_appointment(BaseModel):
    """Book an appointment. Only call after the user has confirmed name, service,
    date and time. Returns a booking reference (APT-...)."""
    patient_name: str = Field(description="Patient's full name as given by the user")
    service: ServiceName
    start_time: str = Field(description="Slot start, ISO 'YYYY-MM-DDTHH:MM', clinic local time")
    notes: str | None = Field(default=None, description="Optional notes, e.g. accessibility needs")


class create_ticket(BaseModel):
    """Create a support ticket for staff follow-up: billing, duplicate invoice,
    insurance help, records request, complaint, reschedule/cancel an existing
    appointment, or other requests the assistant cannot complete itself."""
    category: Literal["billing", "insurance", "records", "complaint", "reschedule_cancel", "other"]
    summary: str = Field(description="What the user needs, with any refs/dates they gave")
    priority: Literal["low", "normal", "high"] = "normal"


class get_order_status(BaseModel):
    """Look up a dental lab order (crowns, aligners, night guards) by order ID (ORD-....)."""
    order_id: str
    last_name: str | None = Field(default=None, description="Patient last name, only if the tool asked for verification")


class handoff_to_human(BaseModel):
    """Transfer the conversation to a human staff member. Use when the user asks
    for a person, is upset, the request needs clinical judgement, or you cannot
    help with the tools and documents available."""
    reason: str


class send_reply(BaseModel):
    """Send your final reply to the user for this turn. Must be called alone, as
    the last step."""
    message: str = Field(description="WhatsApp-friendly reply, under ~120 words, plain text")
    kind: Literal["answer", "action_result", "clarify", "smalltalk"] = Field(
        description="answer = facts from clinic docs; action_result = reports a tool action "
                    "(booking, ticket, order status, availability); clarify = asking the user "
                    "for missing info; smalltalk = greeting/thanks with no clinic facts")
    source_ids: list[str] = Field(default_factory=list,
                                  description="ids of passages from search_clinic_docs that support the answer")
    confidence: Literal["high", "medium", "low"]


LLM_TOOLS = [search_clinic_docs, check_availability, book_appointment, create_ticket,
             get_order_status, handoff_to_human, send_reply]
CONTROL_TOOLS = {"send_reply", "handoff_to_human"}
ACTION_TOOLS = {"check_availability", "book_appointment", "create_ticket", "get_order_status"}


# --- execution ---------------------------------------------------------------

@dataclass
class ToolContext:
    user_id: str
    thread_id: str
    retriever: Any


@dataclass
class ToolOutcome:
    name: str
    ok: bool
    result: Any          # JSON-able; becomes the ToolMessage content
    error: str | None = None


def _search(ctx: ToolContext, a: search_clinic_docs) -> dict:
    from app.config import get_settings
    hits = ctx.retriever.search(a.query, get_settings().top_k)
    return {
        "relevance_threshold": ctx.retriever.min_score,
        "passages": [{"id": h.chunk.id, "score": h.score, "text": h.chunk.render()} for h in hits],
    }


_HANDLERS: dict[str, tuple[type[BaseModel], Callable[[ToolContext, Any], Any]]] = {
    "search_clinic_docs": (search_clinic_docs, _search),
    "check_availability": (check_availability,
                           lambda c, a: backend.check_availability(a.service, a.date)),
    "book_appointment": (book_appointment,
                         lambda c, a: backend.book_appointment(c.user_id, a.patient_name, a.service,
                                                               a.start_time, a.notes)),
    "create_ticket": (create_ticket,
                      lambda c, a: backend.create_ticket(c.user_id, a.category, a.summary,
                                                         a.priority, c.thread_id)),
    "get_order_status": (get_order_status,
                         lambda c, a: backend.get_order_status(c.user_id, a.order_id, a.last_name)),
}


def run_tool(ctx: ToolContext, name: str, args: dict) -> ToolOutcome:
    started = time.perf_counter()
    outcome: ToolOutcome
    if name not in _HANDLERS:
        outcome = ToolOutcome(name, False, {"error": f"Unknown tool '{name}'"}, "unknown_tool")
    else:
        schema, fn = _HANDLERS[name]
        try:
            parsed = schema.model_validate(args or {})
            outcome = ToolOutcome(name, True, fn(ctx, parsed))
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            outcome = ToolOutcome(name, False, {"error": f"Invalid arguments: {msg}"}, "validation")
        except backend.BackendError as exc:
            outcome = ToolOutcome(name, False, {"error": str(exc)}, "business_rule")
        except Exception as exc:  # unexpected: never leak internals to the model/user
            logger.exception("tool crashed", extra={"tool": name, "thread_id": ctx.thread_id})
            outcome = ToolOutcome(name, False,
                                  {"error": "The system had a temporary problem running this action."},
                                  f"exception:{type(exc).__name__}")
    latency = int((time.perf_counter() - started) * 1000)
    try:
        db.log_tool_call(ctx.thread_id, name, args, outcome.result, outcome.ok, outcome.error, latency)
    except Exception:
        logger.exception("failed to persist tool call log")
    logger.info("tool_call", extra={"tool": name, "ok": outcome.ok, "error": outcome.error,
                                    "latency_ms": latency, "thread_id": ctx.thread_id})
    return outcome
