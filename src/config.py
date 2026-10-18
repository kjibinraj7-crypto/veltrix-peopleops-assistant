"""
src/config.py
─────────────
Central configuration module. Reads all settings from environment variables.
Also provides the LLM provider factory that returns the correct SDK client.

Rules:
- All settings come from env vars — never hard-coded.
- LLM_PROVIDER selects which API to call; we support: openai, anthropic, google, groq
  and openai_compatible (any OpenAI-compatible endpoint, e.g. xAI Grok, OpenRouter,
  Together, Ollama, vLLM) via LLM_BASE_URL.
- Groq/xAI/Ollama reuse the OpenAI SDK (OpenAI-compatible REST APIs).
- Token counting always uses tiktoken cl100k_base regardless of provider.

Default model IDs are the cheapest current production model per provider as of
2026-10. Every one can be overridden with LLM_MODEL, so no provider is a dead end.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All application settings, sourced exclusively from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Data paths ────────────────────────────────────────────────────────────
    corpus_dir: str = "./data/corpus"
    manifest_path: str = "./data/manifest.json"
    db_path: str = "./data/veltrix.db"
    vector_store_dir: str = "./vector_store"

    # ── Date override ─────────────────────────────────────────────────────────
    # Set APP_TODAY=YYYY-MM-DD to fix "today" for deterministic testing.
    # Leave empty to use the real system date.
    app_today: str = ""

    # ── LLM provider ─────────────────────────────────────────────────────────
    # openai | anthropic | google | groq | openai_compatible
    llm_provider: str = "openai"
    llm_model: str = ""           # Empty → provider default (see get_default_model)
    llm_temperature: float = 0.0  # Temperature 0 for consistency across test runs
    # Custom endpoint for an OpenAI-compatible API (xAI Grok, OpenRouter, Ollama,
    # vLLM, Together, …). Also lets you point `openai`/`groq` at a proxy.
    llm_base_url: str = ""

    # ── API keys ──────────────────────────────────────────────────────────────
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""
    groq_api_key: str = ""
    # Key used when LLM_PROVIDER=openai_compatible (falls back to OPENAI_API_KEY)
    llm_api_key: str = ""

    @field_validator(
        "openai_api_key", "anthropic_api_key", "google_api_key",
        "groq_api_key", "llm_api_key", mode="before",
    )
    @classmethod
    def _strip_api_keys(cls, v: Any) -> Any:
        """Whitespace-tolerant: a paste that leaves `KEY= value` must still work."""
        return v.strip() if isinstance(v, str) else v

    # ── Embedding ─────────────────────────────────────────────────────────────
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # BGE query prefix — must be prepended to queries at retrieval time (not documents)
    bge_query_prefix: str = "Represent this sentence for searching relevant passages: "

    # ── RAG / retrieval ───────────────────────────────────────────────────────
    retrieval_top_k: int = 30          # Candidates from FAISS + BM25 before filtering
    corpus_token_budget: int = 3000    # Max corpus tokens per LLM call (task requirement)
    confidence_threshold: float = 0.30 # Below this → return no_answer without LLM call

    # ── Performance ───────────────────────────────────────────────────────────
    # Per-attempt timeout for a single LLM HTTP call, and a hard ceiling for the
    # whole request. Section 9 requires every request to finish within 30 seconds,
    # so the request ceiling is deliberately below that.
    llm_rate_limit_retries: int = 1   # Short in-request retry on provider throttling (429)
    llm_rate_limit_max_sleep: float = 8.0  # Cap for that retry wait, in seconds
    llm_timeout_seconds: int = 20
    llm_max_retries: int = 0
    request_timeout_seconds: int = 25
    cache_ttl_seconds: int = 604800       # Semantic cache TTL (7 days; invalidated by corpus change)
    semantic_cache_enabled: bool = True      # Set false for a cold run (no reuse at all)
    semantic_cache_threshold: float = 0.93  # Min cosine similarity to reuse an answer
    semantic_cache_max_entries: int = 500   # Bound on locally stored answers

    # ── Ingestion integrity ───────────────────────────────────────────────────
    # Pages with fewer than this many extracted characters are treated as
    # suspicious: they are re-tried with OCR and, if still thin, flagged in the
    # ingestion report instead of being silently dropped.
    min_page_chars: int = 200
    min_document_chars: int = 200

    # ── Testing / ops ─────────────────────────────────────────────────────────
    # When true, startup does NOT run the ingestion pipeline. Used by the test
    # suite (which seeds its own DB and does not need a vector index) and by
    # local API-only development. Never set this in the evaluated container.
    skip_ingestion: bool = False

    def get_today(self) -> date:
        """
        Return the reference date for today.
        Uses APP_TODAY env var if set (for testing), otherwise the real system date.
        """
        if self.app_today:
            return datetime.strptime(self.app_today, "%Y-%m-%d").date()
        return date.today()

    def get_default_model(self) -> str:
        """Return the default model name for the configured provider."""
        defaults = {
            "openai":            "gpt-6-luna",
            "anthropic":         "claude-haiku-4-5-20251001",
            "google":            "gemini-3.5-flash-lite",
            "groq":              "openai/gpt-oss-120b",
            "openai_compatible": "",   # must be supplied via LLM_MODEL
        }
        if self.llm_model:
            return self.llm_model
        return defaults.get(self.llm_provider.lower().strip(), "")

    def get_llm_client(self) -> Any:
        """
        Factory: return the correct LLM SDK client for the configured provider.

        - openai            → openai.OpenAI
        - groq              → openai.OpenAI with the Groq base_url (OpenAI-compatible)
        - openai_compatible → openai.OpenAI with LLM_BASE_URL (xAI Grok, OpenRouter,
                              Ollama, vLLM, Together, …)
        - anthropic         → anthropic.Anthropic
        - google            → google-genai Client

        IMPORTANT: Token counting always uses tiktoken cl100k_base regardless of provider.
        """
        provider = self.llm_provider.lower().strip()

        if provider in ("openai", "groq", "openai_compatible"):
            from openai import OpenAI

            base_url = self.llm_base_url or None
            if provider == "groq" and not base_url:
                base_url = "https://api.groq.com/openai/v1"
            if provider == "openai_compatible" and not base_url:
                raise ValueError(
                    "LLM_PROVIDER=openai_compatible requires LLM_BASE_URL "
                    "(for example https://api.x.ai/v1 for xAI Grok)."
                )

            if provider == "openai":
                api_key, key_var = self.openai_api_key, "OPENAI_API_KEY"
            elif provider == "groq":
                api_key, key_var = self.groq_api_key, "GROQ_API_KEY"
            else:
                api_key = self.llm_api_key or self.openai_api_key
                key_var = "LLM_API_KEY"

            if not api_key:
                raise ValueError(
                    f"No API key configured for LLM_PROVIDER={provider}. "
                    f"Set {key_var} in .env."
                )

            return OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=self.llm_timeout_seconds,
                max_retries=self.llm_max_retries,
            )

        elif provider == "anthropic":
            from anthropic import Anthropic
            if not self.anthropic_api_key:
                raise ValueError(
                    "No API key configured for LLM_PROVIDER=anthropic. "
                    "Set ANTHROPIC_API_KEY in .env."
                )
            return Anthropic(
                api_key=self.anthropic_api_key,
                timeout=self.llm_timeout_seconds,
                max_retries=self.llm_max_retries,
            )

        elif provider == "google":
            from google import genai
            if not self.google_api_key:
                raise ValueError(
                    "No API key configured for LLM_PROVIDER=google. "
                    "Set GOOGLE_API_KEY in .env."
                )
            return genai.Client(api_key=self.google_api_key)

        else:
            raise ValueError(
                f"Unsupported LLM_PROVIDER: '{provider}'. Choose from: "
                "openai, anthropic, google, groq, openai_compatible"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance.
    Cached after first call — env vars are read once at startup.
    """
    return Settings()
