"""
tests/test_semantic_cache.py
────────────────────────────
Tests for the semantic response cache:
  * exact and semantic (near-duplicate) reuse
  * scope isolation (one employee can never be served another's answer)
  * route exclusion (live tool/leave data is never cached)
  * corpus-fingerprint invalidation (updated documents ⇒ old answers unused)
  * local persistence across restarts
"""
from __future__ import annotations

import numpy as np
import pytest

from src.orchestration.cache import SemanticResponseCache


# ── A controllable stand-in for the real embedding model ─────────────────────
_VECTORS: dict[str, list[float]] = {}


def _fake_embed(text: str):
    if text in _VECTORS:
        v = np.asarray(_VECTORS[text], dtype=np.float32)
    else:
        # unique-ish deterministic vector for unseen text
        seed = float(sum(ord(c) for c in text) % 997) / 997.0
        v = np.asarray([seed, 1.0 - seed, 0.0, 0.0], dtype=np.float32)
    n = float(np.linalg.norm(v)) or 1.0
    return v / n


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    _VECTORS.clear()
    monkeypatch.setattr("src.ingestion.embedder.embed_query", _fake_embed)
    yield
    _VECTORS.clear()


def _rag(answer: str = "12 days") -> dict:
    return {"route": "rag", "answer": answer, "citations": [], "tool_calls": []}


@pytest.fixture
def cache(tmp_path):
    c = SemanticResponseCache(ttl_seconds=3600, threshold=0.95, store_path=str(tmp_path / "c.json"))
    c.set_corpus_fingerprint("corpus-hash-1")
    return c


def test_exact_repeat_is_reused(cache):
    cache.set("E1", "employee", "IN", "How many sick days do I get?", _rag())
    hit = cache.get("E1", "employee", "IN", "How many sick days do I get?")
    assert hit is not None and hit["answer"] == "12 days"


def test_semantically_similar_question_is_reused(cache):
    _VECTORS["How many sick days do I get?"] = [1.0, 0.0, 0.0, 0.0]
    _VECTORS["sick leave entitlement please"] = [0.999, 0.01, 0.0, 0.0]
    cache.set("E1", "employee", "IN", "How many sick days do I get?", _rag())
    hit = cache.get("E1", "employee", "IN", "sick leave entitlement please")
    assert hit is not None and hit["answer"] == "12 days"


def test_dissimilar_question_is_a_miss(cache):
    _VECTORS["How many sick days do I get?"] = [1.0, 0.0, 0.0, 0.0]
    _VECTORS["How do I apply for leave?"] = [0.0, 1.0, 0.0, 0.0]
    cache.set("E1", "employee", "IN", "How many sick days do I get?", _rag())
    assert cache.get("E1", "employee", "IN", "How do I apply for leave?") is None


def test_answers_never_cross_employees(cache):
    cache.set("E1", "employee", "IN", "How many sick days do I get?", _rag("12 days"))
    # Same question, different employee → must NOT reuse (avoids data leakage).
    assert cache.get("E2", "employee", "IN", "How many sick days do I get?") is None
    # Different role → also a miss.
    assert cache.get("E1", "hr", "IN", "How many sick days do I get?") is None


def test_live_tool_answers_are_not_cached(cache):
    cache.set("E1", "employee", "IN", "my leave balance", {"route": "tool", "answer": "9 days"})
    assert cache.get("E1", "employee", "IN", "my leave balance") is None
    cache.set("E1", "employee", "IN", "hybrid q", {"route": "hybrid", "answer": "x"})
    assert cache.get("E1", "employee", "IN", "hybrid q") is None


def test_negative_answers_are_not_entrenched(cache):
    """A no_answer must not be replayed for the whole TTL after a fix."""
    cache.set("E1", "employee", "IN", "uncovered q", {"route": "no_answer", "answer": "no idea"})
    assert cache.get("E1", "employee", "IN", "uncovered q") is None


def test_updated_corpus_invalidates_every_stored_answer(cache):
    cache.set("E1", "employee", "IN", "How many sick days do I get?", _rag())
    assert cache.get("E1", "employee", "IN", "How many sick days do I get?") is not None
    # Documents were updated → new fingerprint → old answer must not be reused.
    cache.set_corpus_fingerprint("corpus-hash-2")
    assert cache.get("E1", "employee", "IN", "How many sick days do I get?") is None
    assert cache.stats_snapshot()["entries"] == 0


def test_nothing_is_cached_without_a_corpus_fingerprint(tmp_path):
    c = SemanticResponseCache(store_path=str(tmp_path / "c.json"))
    c.set("E1", "employee", "IN", "q", _rag())
    assert c.get("E1", "employee", "IN", "q") is None


def test_disabled_cache_never_reuses(tmp_path):
    c = SemanticResponseCache(store_path=str(tmp_path / "c.json"), enabled=False)
    c.set_corpus_fingerprint("h")
    c.set("E1", "employee", "IN", "q", _rag())
    assert c.get("E1", "employee", "IN", "q") is None
    assert c.stats_snapshot()["entries"] == 0


def test_cached_file_is_not_trusted_without_ingestion(tmp_path):
    """
    A cache file on disk must not become usable just because it says which corpus
    it belongs to. Only the ingestion pipeline may declare the served corpus, so
    a process that never ingested (e.g. SKIP_INGESTION) cannot replay an answer
    produced from documents that may since have changed.
    """
    path = str(tmp_path / "c.json")
    first = SemanticResponseCache(store_path=path)
    first.set_corpus_fingerprint("corpus-hash-1")
    first.set("E1", "employee", "IN", "q", _rag())
    first.save()

    # New process that never runs ingestion → no fingerprint declared.
    second = SemanticResponseCache(store_path=path)
    assert second.get("E1", "employee", "IN", "q") is None


def test_answers_persist_across_restarts(tmp_path):
    path = str(tmp_path / "c.json")
    first = SemanticResponseCache(ttl_seconds=3600, store_path=path)
    first.set_corpus_fingerprint("corpus-hash-1")
    first.set("E1", "employee", "IN", "How many sick days do I get?", _rag())
    first.save()

    second = SemanticResponseCache(ttl_seconds=3600, store_path=path)
    second.set_corpus_fingerprint("corpus-hash-1")
    hit = second.get("E1", "employee", "IN", "How many sick days do I get?")
    assert hit is not None and hit["answer"] == "12 days"


def test_persisted_answers_are_dropped_when_corpus_changed(tmp_path):
    path = str(tmp_path / "c.json")
    first = SemanticResponseCache(ttl_seconds=3600, store_path=path)
    first.set_corpus_fingerprint("corpus-hash-1")
    first.set("E1", "employee", "IN", "q", _rag())
    first.save()

    second = SemanticResponseCache(ttl_seconds=3600, store_path=path)
    second.set_corpus_fingerprint("corpus-hash-2")
    assert second.get("E1", "employee", "IN", "q") is None


def test_cached_response_is_a_copy(cache):
    cache.set("E1", "employee", "IN", "q", _rag("original"))
    first = cache.get("E1", "employee", "IN", "q")
    first["answer"] = "tampered"
    second = cache.get("E1", "employee", "IN", "q")
    assert second["answer"] == "original"


def test_expired_entry_is_not_reused(tmp_path):
    c = SemanticResponseCache(ttl_seconds=-1, store_path=str(tmp_path / "c.json"))
    c.set_corpus_fingerprint("h")
    c.set("E1", "employee", "IN", "q", _rag())
    assert c.get("E1", "employee", "IN", "q") is None


def test_stats_track_hits(cache):
    cache.set("E1", "employee", "IN", "q", _rag())
    cache.get("E1", "employee", "IN", "q")
    snap = cache.stats_snapshot()
    assert snap["hits"] == 1 and snap["stores"] == 1 and snap["entries"] == 1
