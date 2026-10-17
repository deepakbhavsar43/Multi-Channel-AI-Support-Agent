"""Chat with the agent in the terminal (no Twilio needed).

    python -m scripts.chat_cli            # uses LLM_MODEL from .env
Type /reset to start a new session, /quit to exit.
"""
from __future__ import annotations

import secrets
import sqlite3

from app import db
from app.agent.graph import build_graph
from app.config import get_settings
from app.logging_setup import setup_logging
from app.main import make_llm
from app.rag.retriever import build_retriever
from app.service import ConversationService
from app.tools import backend


def main() -> None:
    s = get_settings()
    setup_logging("WARNING")
    db.init_db()
    backend.seed_demo_data()
    from langgraph.checkpoint.sqlite import SqliteSaver
    saver = SqliteSaver(sqlite3.connect(s.checkpoint_db_path, check_same_thread=False))
    svc = ConversationService(build_graph(make_llm(s), build_retriever(s), saver))
    user = f"cli:{secrets.token_hex(3)}"
    print(f"Lotus Lane Dental assistant ({s.llm_model}). /reset, /quit")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text == "/quit":
            break
        if text == "/reset":
            user = f"cli:{secrets.token_hex(3)}"
            print("(new session)")
            continue
        reply = svc.handle_message("cli", user, text)
        if reply and reply.text:
            print(f"bot> {reply.text}\n     [{reply.meta}]")


if __name__ == "__main__":
    main()
