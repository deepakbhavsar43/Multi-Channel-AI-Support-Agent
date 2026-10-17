from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import db
from app.agent.graph import build_graph
from app.config import get_settings
from app.rag.retriever import build_retriever
from app.service import ConversationService
from app.tools import backend

# Wednesday 14 Oct 2026, 10:00 clinic time
FIXED_NOW = datetime(2026, 10, 14, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("RETRIEVER", "bm25")
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    # Isolate tests from a developer's real .env: no real WhatsApp provider or public URL.
    for var in ("WHATSAPP_PROVIDER", "META_ACCESS_TOKEN", "META_PHONE_NUMBER_ID", "META_APP_SECRET",
                "META_VERIFY_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                "TWILIO_WHATSAPP_FROM", "PUBLIC_BASE_URL"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    monkeypatch.setattr(backend, "now", lambda: FIXED_NOW)
    db.init_db()
    backend.seed_demo_data()
    yield
    get_settings.cache_clear()


@pytest.fixture
def retriever():
    return build_retriever(get_settings())


@pytest.fixture
def make_service(retriever):
    def _make(llm):
        return ConversationService(build_graph(llm, retriever, InMemorySaver()))
    return _make
