"""Scripted chat model for offline tests: returns queued AIMessages in order.

Each script item is either an AIMessage or a callable(messages) -> AIMessage,
so a step can react to tool results (e.g. cite the ids that search returned).
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

_ids = itertools.count()


def call(name: str, **args: Any) -> dict:
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def ai(*calls: dict, content: str = "") -> AIMessage:
    return AIMessage(content=content, tool_calls=list(calls))


def last_search_ids(messages) -> list[str]:
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == "search_clinic_docs":
            return [p["id"] for p in json.loads(m.content)["passages"]]
    return []


def last_tool_result(messages, name: str) -> dict:
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and m.name == name:
            return json.loads(m.content)
    raise AssertionError(f"no {name} result")


class ScriptedLLM(BaseChatModel):
    script: list[Any]
    seen: list[list] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        if not self.script:
            raise AssertionError("ScriptedLLM ran out of responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = item(messages) if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


Step = AIMessage | Callable[[list], AIMessage] | Exception
