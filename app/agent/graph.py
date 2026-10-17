"""LangGraph agent: guard -> agent <-> tools -> verify -> (reply | retry | handoff).

    START ─▶ guard ──pass──▶ agent ──tool calls──▶ tools ──▶ agent
               │                │
               │                ├─send_reply──▶ verify ──ok──▶ END
               │                │                 ├─fail once──▶ agent (self-correct)
               │                │                 └─fail again / low confidence──▶ handoff
               │                └─handoff_to_human / step limit ─────────────────▶ handoff ─▶ END
               ├─human request / emergency ─────────────────────────────────────▶ handoff
               └─sensitive data / bad input ──▶ END (fixed reply)

Memory: the graph is compiled with a checkpointer and invoked with
thread_id = the user's current session, so `messages` carries the conversation.
Per-turn bookkeeping fields are reset by `guard` at the start of every turn.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, SystemMessage,
                                     ToolMessage)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app import db
from app.agent import guard as guard_mod
from app.agent import prompts
from app.config import get_settings
from app.tools import backend
from app.tools.registry import ACTION_TOOLS, LLM_TOOLS, ToolContext, run_tool

logger = logging.getLogger(__name__)

HISTORY_TURNS = 8          # user turns of history sent to the LLM
MAX_TOOL_FAILURES = 3      # per turn, before escalating
REF_PATTERN = re.compile(r"\b(?:APT|TKT|ORD)-[A-Z0-9]{3,}\b")
MONEY_PATTERN = re.compile(r"(₹|\brs\.?\s?\d|\binr\b)", re.I)
AMOUNT_PATTERN = re.compile(r"(?:₹|\brs\.?|\binr)\s?([\d,]+)", re.I)
EMERGENCY_FEE_PATTERN = re.compile(r"emergency\s+(fee|charge)|₹\s?700\b", re.I)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    user_input: str
    # per-turn bookkeeping
    guard_action: str
    retrieved: dict[str, float]
    action_results: list[dict]
    steps: int
    retries: int
    tool_failures: int
    handoff_reason: str | None
    handoff_priority: str
    reply: str | None
    reply_meta: dict
    handoff_ticket: str | None


def _ctx(config: RunnableConfig) -> tuple[str, str]:
    c = config.get("configurable", {})
    return c["user_id"], c["thread_id"]


def _trim(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Keep the last N user turns, cutting only at a HumanMessage so tool-call
    / tool-result pairs are never split."""
    human_idx = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_idx) <= HISTORY_TURNS:
        return messages
    return messages[human_idx[-HISTORY_TURNS]:]


def _pending_calls(state: AgentState) -> list[dict]:
    last = state["messages"][-1] if state.get("messages") else None
    return list(getattr(last, "tool_calls", None) or [])


def build_graph(llm, retriever, checkpointer=None):
    settings = get_settings()
    model = llm.bind_tools(LLM_TOOLS, tool_choice="any")

    # ---------------------------------------------------------------- guard
    def guard(state: AgentState, config: RunnableConfig) -> dict:
        user_id, thread_id = _ctx(config)
        result = guard_mod.check(state.get("user_input", ""), settings.max_input_chars)
        update: dict[str, Any] = {
            "guard_action": result.action, "retrieved": {}, "action_results": [], "steps": 0,
            "retries": 0, "tool_failures": 0, "handoff_reason": None, "handoff_priority": "normal",
            "reply": None, "reply_meta": {}, "handoff_ticket": None,
            "messages": [HumanMessage(result.clean_text or "(empty message)")],
        }
        if result.action != "pass":
            db.log_event("guard", thread_id, user_id, action=result.action)
        if result.action == "emergency":
            update |= {"handoff_reason": "emergency_keywords", "handoff_priority": "urgent",
                       "reply": result.reply}
        elif result.action == "human":
            update |= {"handoff_reason": "user_requested", "handoff_priority": "normal"}
        elif result.action in ("sensitive", "reject"):
            update |= {"reply": result.reply, "reply_meta": {"kind": "guard"},
                       "messages": update["messages"] + [AIMessage(result.reply)]}
        return update

    def route_guard(state: AgentState) -> Literal["agent", "handoff", "__end__"]:
        action = state["guard_action"]
        if action == "pass":
            return "agent"
        if action in ("emergency", "human"):
            return "handoff"
        return END

    # ---------------------------------------------------------------- agent
    def agent(state: AgentState, config: RunnableConfig) -> dict:
        steps = state.get("steps", 0) + 1
        if steps > settings.max_agent_steps:
            return {"steps": steps, "handoff_reason": "step_limit"}
        sys = SystemMessage(prompts.system_prompt(backend.now(), settings.clinic_timezone))
        response = model.invoke([sys, *_trim(state["messages"])], config)
        return {"steps": steps, "messages": [response]}

    def route_agent(state: AgentState) -> Literal["tools", "verify", "handoff"]:
        if state.get("handoff_reason"):
            return "handoff"
        names = {c["name"] for c in _pending_calls(state)}
        if "handoff_to_human" in names:
            return "handoff"
        if names - {"send_reply"}:
            return "tools"
        return "verify"  # send_reply, or plain text the model produced anyway

    # ---------------------------------------------------------------- tools
    def tools(state: AgentState, config: RunnableConfig) -> dict:
        user_id, thread_id = _ctx(config)
        ctx = ToolContext(user_id=user_id, thread_id=thread_id, retriever=retriever)
        retrieved = dict(state.get("retrieved", {}))
        actions = list(state.get("action_results", []))
        failures = state.get("tool_failures", 0)
        out: list[ToolMessage] = []
        for call in _pending_calls(state):
            if call["name"] == "send_reply":
                out.append(ToolMessage("Ignored: call send_reply alone, after reading tool results.",
                                       tool_call_id=call["id"], status="error"))
                continue
            outcome = run_tool(ctx, call["name"], call.get("args", {}))
            if outcome.ok and call["name"] == "search_clinic_docs":
                for p in outcome.result["passages"]:
                    retrieved[p["id"]] = max(p["score"], retrieved.get(p["id"], float("-inf")))
            elif outcome.ok and call["name"] in ACTION_TOOLS:
                actions.append({"tool": call["name"], "result": outcome.result})
            if not outcome.ok:
                failures += 1
            out.append(ToolMessage(json.dumps(outcome.result, ensure_ascii=False, default=str),
                                   tool_call_id=call["id"], name=call["name"],
                                   status="success" if outcome.ok else "error"))
        update: dict[str, Any] = {"messages": out, "retrieved": retrieved,
                                  "action_results": actions, "tool_failures": failures}
        if failures >= MAX_TOOL_FAILURES:
            update["handoff_reason"] = "repeated_tool_failures"
        return update

    def route_tools(state: AgentState) -> Literal["agent", "handoff"]:
        return "handoff" if state.get("handoff_reason") else "agent"

    # ---------------------------------------------------------------- verify
    def _reply_meta(state: AgentState, args: dict) -> dict:
        """What the UI shows next to a reply: cited sections (with readable labels),
        confidence, and the structured results of actions taken this turn."""
        chunks = getattr(retriever, "chunks_by_id", {})
        sources = list(args.get("source_ids", []))
        meta: dict[str, Any] = {"kind": args.get("kind"), "sources": sources,
                                "confidence": args.get("confidence")}
        labels = {s: f"{chunks[s].title} › {chunks[s].section}" for s in sources if s in chunks}
        if labels:
            meta["source_labels"] = labels
        if state.get("action_results"):
            meta["actions"] = state["action_results"]
        return meta

    def _check_reply(state: AgentState, args: dict) -> str | None:
        """Return a rejection reason, or None if the reply may be sent."""
        message = (args.get("message") or "").strip()
        kind = args.get("kind", "clarify")
        if not message:
            return "empty message"
        retrieved = state.get("retrieved", {})
        actions = state.get("action_results", [])
        if kind == "answer":
            sources = [s for s in args.get("source_ids", []) if s in retrieved]
            if not args.get("source_ids"):
                return "an answer about the clinic must cite source_ids from search_clinic_docs"
            if len(sources) != len(args["source_ids"]):
                return "source_ids include passages that were not retrieved in this turn"
            if max(retrieved[s] for s in sources) < retriever.min_score:
                return "cited passages are below the relevance threshold"
        if kind == "action_result" and not actions:
            return "reports an action but no action tool succeeded in this turn"
        known = json.dumps(actions, default=str) + state.get("user_input", "")
        for ref in REF_PATTERN.findall(message):
            if ref not in known:
                return f"mentions reference {ref} that no tool returned"
        # A non-emergency booking must never quote the emergency fee (it is only
        # charged for same-day emergency visits; tools return the real fee).
        for act in actions:
            res = act.get("result") or {}
            if (act.get("tool") == "book_appointment" and res.get("service") != "Emergency visit"
                    and EMERGENCY_FEE_PATTERN.search(message)):
                return ("quotes the emergency fee for a non-emergency booking; use the 'fee' "
                        "returned by book_appointment")
        if kind in ("clarify", "smalltalk") and MONEY_PATTERN.search(message):
            # Prices are fine when a tool returned them this turn (e.g. availability + fee).
            amounts = [a.replace(",", "") for a in AMOUNT_PATTERN.findall(message)]
            tool_text = json.dumps(actions, default=str, ensure_ascii=False).replace(",", "")
            if not amounts or any(a not in tool_text for a in amounts):
                return "states prices without citing clinic documents"
        return None

    def verify(state: AgentState, config: RunnableConfig) -> dict:
        user_id, thread_id = _ctx(config)
        last = state["messages"][-1]
        call = next((c for c in _pending_calls(state) if c["name"] == "send_reply"), None)
        args = call["args"] if call else {"message": last.text if hasattr(last, "text") else str(last.content),
                                          "kind": "clarify", "confidence": "medium"}
        if args.get("confidence") == "low":
            db.log_event("low_confidence", thread_id, user_id, draft=args.get("message"))
            return {"handoff_reason": "low_confidence"}

        reason = _check_reply(state, args)
        if reason is None:
            ack = [ToolMessage("Delivered to user.", tool_call_id=call["id"])] if call else []
            return {"messages": ack, "reply": args["message"].strip(),
                    "reply_meta": _reply_meta(state, args)}

        db.log_event("grounding_reject", thread_id, user_id, reason=reason,
                     draft=args.get("message"), retry=state.get("retries", 0))
        logger.warning("reply rejected", extra={"thread_id": thread_id, "reason": reason})
        if state.get("retries", 0) >= 1:
            return {"handoff_reason": f"grounding_failed: {reason}"}
        note = prompts.RETRY_NOTE.format(reason=reason)
        fix = (ToolMessage(note, tool_call_id=call["id"], status="error") if call
               else HumanMessage(f"(automated check) {note}"))
        return {"messages": [fix], "retries": state.get("retries", 0) + 1}

    def route_verify(state: AgentState) -> Literal["agent", "handoff", "__end__"]:
        if state.get("handoff_reason"):
            return "handoff"
        return END if state.get("reply") else "agent"

    # ---------------------------------------------------------------- handoff
    def handoff(state: AgentState, config: RunnableConfig) -> dict:
        user_id, thread_id = _ctx(config)
        reason = state.get("handoff_reason") or "agent_requested"
        closing: list[AnyMessage] = []
        for call in _pending_calls(state):  # every tool call needs a result for valid history
            if call["name"] == "handoff_to_human":
                reason = f"agent_requested: {call['args'].get('reason', '')}".strip()
            closing.append(ToolMessage("Conversation transferred to a human.", tool_call_id=call["id"]))
        recent = [m.content for m in state["messages"] if isinstance(m, HumanMessage)][-4:]
        summary = f"Handoff reason: {reason}\nRecent user messages:\n" + "\n".join(f"- {t}" for t in recent)
        ticket = backend.create_ticket(user_id, "emergency" if state.get("handoff_priority") == "urgent"
                                       else "handoff", summary,
                                       "high" if state.get("handoff_priority") == "urgent" else "normal",
                                       thread_id)["ticket_ref"]
        db.log_event("handoff", thread_id, user_id, reason=reason, ticket=ticket)
        logger.info("handoff", extra={"thread_id": thread_id, "reason": reason, "ticket": ticket})
        if state.get("handoff_priority") == "urgent" and state.get("reply"):
            reply = f"{state['reply']} Reference: {ticket}."
        else:
            reply = prompts.HANDOFF_REPLY.format(ticket=ticket)
        return {"messages": closing + [AIMessage(reply)], "reply": reply, "handoff_ticket": ticket,
                "handoff_reason": reason,
                "reply_meta": {"kind": "handoff", "reason": reason, "ticket": ticket,
                               "priority": state.get("handoff_priority", "normal")}}

    g = StateGraph(AgentState)
    for name, fn in [("guard", guard), ("agent", agent), ("tools", tools),
                     ("verify", verify), ("handoff", handoff)]:
        g.add_node(name, fn)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", route_guard)
    g.add_conditional_edges("agent", route_agent)
    g.add_conditional_edges("tools", route_tools)
    g.add_conditional_edges("verify", route_verify)
    g.add_edge("handoff", END)
    return g.compile(checkpointer=checkpointer)
