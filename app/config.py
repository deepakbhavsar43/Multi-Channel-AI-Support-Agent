"""Application settings, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent

# Export .env into os.environ so provider SDKs (GOOGLE_API_KEY, GROQ_API_KEY,
# OPENAI_API_KEY ...) see their keys. Real environment variables win.
load_dotenv(ROOT_DIR / ".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    # --- LLM -------------------------------------------------------------
    # Any tool-calling chat model supported by langchain's init_chat_model,
    # written as "provider:model", e.g. "google_genai:gemini-3.5-flash-lite"
    # (free key), "groq:llama-3.3-70b-versatile" (free key), "ollama:qwen2.5:7b"
    # (local, no key), "openai:gpt-4.1-mini" or "anthropic:claude-haiku-4-5".
    # The provider's API key env var must be set (GOOGLE_API_KEY, GROQ_API_KEY ...).
    llm_model: str = "google_genai:gemini-3.5-flash-lite"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 25.0
    llm_max_retries: int = 2

    # --- RAG -------------------------------------------------------------
    docs_dir: Path = ROOT_DIR / "data" / "docs"
    index_path: Path = ROOT_DIR / "data" / "index.json"
    # "dense" uses fastembed (local ONNX model, no API key); falls back to
    # "bm25" automatically if the embedding model cannot be loaded.
    retriever: str = "dense"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    top_k: int = 4
    # Minimum best-match score for a question to count as answerable.
    min_score_dense: float = 0.55
    min_score_bm25: float = 2.0

    # --- Agent behaviour -------------------------------------------------
    max_agent_steps: int = 6          # LLM<->tool round trips per user turn
    max_failures_before_handoff: int = 2
    session_timeout_min: int = 30     # new memory thread after inactivity
    handoff_idle_timeout_min: int = 30  # bot takes back a handoff once staff go quiet this long
    max_input_chars: int = 1500
    clinic_timezone: str = "Asia/Kolkata"

    # --- Storage -----------------------------------------------------------
    db_path: Path = ROOT_DIR / "data" / "app.db"
    checkpoint_db_path: Path = ROOT_DIR / "data" / "checkpoints.db"

    # --- Twilio ----------------------------------------------------------
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""     # e.g. "whatsapp:+14155238886" (sandbox)
    twilio_validate_signature: bool = True
    # Public https URL of this service (ngrok / Render). Twilio signature
    # validation needs it behind proxies; "https://" is added if missing.
    public_base_url: str = ""

    # --- WhatsApp provider ---------------------------------------------------
    # "auto" uses Meta Cloud API when META_ACCESS_TOKEN + META_PHONE_NUMBER_ID
    # are set, otherwise Twilio. Force with "meta" or "twilio".
    whatsapp_provider: str = "auto"

    # --- Meta WhatsApp Cloud API -------------------------------------------
    meta_access_token: str = ""        # App Dashboard > WhatsApp > API Setup (or a system user token)
    meta_phone_number_id: str = ""     # "Phone number ID" on the API Setup page (not the phone number)
    meta_app_secret: str = ""          # App settings > Basic > App secret; verifies webhook signatures
    meta_verify_token: str = ""        # any string you invent; must match the dashboard's Verify token
    meta_graph_version: str = "v23.0"
    meta_validate_signature: bool = True

    # --- Admin -----------------------------------------------------------
    admin_token: str = Field(default="", description="Bearer token for /admin endpoints")

    log_level: str = "INFO"


def active_whatsapp_provider(s: "Settings") -> str | None:
    """Which WhatsApp provider sends replies: "meta", "twilio" or None."""
    meta_ok = bool(s.meta_access_token and s.meta_phone_number_id)
    twilio_ok = bool(s.twilio_account_sid and s.twilio_auth_token and s.twilio_whatsapp_from)
    choice = s.whatsapp_provider.strip().lower()
    if choice == "meta":
        return "meta" if meta_ok else None
    if choice == "twilio":
        return "twilio" if twilio_ok else None
    return "meta" if meta_ok else ("twilio" if twilio_ok else None)


@lru_cache
def get_settings() -> Settings:
    return Settings()
