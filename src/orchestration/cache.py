"""
src/orchestration/cache.py
──────────────────────────
Semantic response cache — reuse a previous answer when a *new* question is
close enough in meaning to one we already answered. This cuts both LLM cost and
latency: a paraphrase of an earlier question is served without retrieval or an
LLM call.

Correctness rules
-----------------
1. **Corpus fingerprint.** Every entry records the hash of the corpus it was
   produced from. If the documents change (different hash), NO stored answer is
   ever reused — the entry is treated as a miss and pruned. This is the "if the
   RAG documents were updated, don't use the old responses" requirement.
2. **Scope.** Entries are keyed by (employee_id, role, region). An answer is
   only ever replayed to the same requester, so a cached answer can never leak
   another employee's data or a role-specific document.
3. **Route.** Only grounded `rag` answers are cached. Tool/hybrid answers carry
   live leave data, and `no_answer` is a negative result that must not be
   entrenched for the TTL — neither is reused.

Storage is local: a JSON file under the vector-store directory
(`vector_store/semantic_cache.json`), so answers survive a process restart.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from src.utils.logging import get_logger

logger = get_logger(__name__)

CACHE_FILENAME = "semantic_cache.json"
# Bump when the entry format changes, so an old file is simply discarded.
CACHE_FORMAT_VERSION = 2


def _normalise(query: str) -> str:
    return " ".join((query or "").lower().split())


@dataclass
class CacheEntry:
    scope: str
    query: str
    vector: list[float]
    response: dict[str, Any]
    route: str
    corpus_fingerprint: str
    created_at: float
    expires_at: float
    hits: int = 0


@dataclass
class CacheStats:
    lookups: int = 0
    hits: int = 0
    stores: int = 0
    invalidated_by_fingerprint: int = 0
    evictions: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


class SemanticResponseCache:
    """Embedding-similarity cache with local persistence and corpus invalidation."""

    def __init__(
        self,
        ttl_seconds: int = 604800,
        threshold: float = 0.93,
        max_entries: int = 500,
        store_path: str | None = None,
        flush_interval_seconds: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._ttl = ttl_seconds
        self._threshold = threshold
        self._max_entries = max_entries
        self._store_path = store_path
        self._flush_interval = flush_interval_seconds
        self._entries: list[CacheEntry] = []
        # Reentrant: internal helpers call save(), which also takes the lock.
        self._lock = threading.RLock()
        self._loaded = False
        self._dirty = False
        self._last_flush = 0.0
        # Set by the ingestion pipeline to the hash of the indexed corpus.
        self._corpus_fingerprint: str = ""
        self._embedder_model: str = ""
        self.stats = CacheStats()

    # ── corpus identity ───────────────────────────────────────────────────────

    def set_corpus_fingerprint(self, fingerprint: str, embedder_model: str | None = None) -> None:
        """
        Record the identity of the currently indexed corpus.

        Called by the ingestion pipeline. If this differs from the fingerprint
        stamped on a stored entry, that entry is never reused.
        """
        changed = bool(fingerprint) and fingerprint != self._corpus_fingerprint
        self._corpus_fingerprint = fingerprint or ""
        if embedder_model:
            self._embedder_model = embedder_model
        with self._lock:
            self._load_locked()
            if changed:
                dropped = self._prune_stale_locked()
                if dropped:
                    self.stats.invalidated_by_fingerprint += dropped
                    logger.info(
                        "Semantic cache invalidated: corpus changed",
                        extra={"dropped_entries": dropped, "fingerprint": fingerprint[:12]},
                    )
            self._dirty = True
        self.save()

    def fingerprint(self) -> str:
        return self._corpus_fingerprint

    # ── persistence ───────────────────────────────────────────────────────────

    def _resolved_path(self) -> str | None:
        if self._store_path:
            return self._store_path
        try:
            from src.config import get_settings
            return os.path.join(get_settings().vector_store_dir, CACHE_FILENAME)
        except Exception:  # noqa: BLE001 — settings unavailable (e.g. unit tests)
            return None

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._resolved_path()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:  # noqa: BLE001 — a corrupt cache is just discarded
            logger.warning("Semantic cache unreadable — starting empty", extra={"error": str(exc)})
            return
        if data.get("format_version") != CACHE_FORMAT_VERSION:
            logger.info("Semantic cache format changed — discarded")
            return
        now = time.time()
        for raw in data.get("entries", []):
            try:
                entry = CacheEntry(**raw)
            except TypeError:
                continue
            if entry.expires_at > now:
                self._entries.append(entry)
        self._embedder_model = data.get("embedder_model", self._embedder_model)
        # NOTE: the fingerprint stored in the file is deliberately NOT adopted.
        # Only the ingestion pipeline may declare which corpus is being served
        # (via set_corpus_fingerprint). If ingestion did not run, the in-memory
        # fingerprint stays empty and every loaded entry fails the fingerprint
        # check in _is_usable() — so a stale answer can never be replayed after
        # the documents changed, in any mode (including SKIP_INGESTION).
        self._prune_stale_locked()
        self._evict_locked()
        logger.info(
            "Semantic cache loaded",
            extra={"entries": len(self._entries), "corpus_fingerprint": self._corpus_fingerprint[:12]},
        )

    def save(self) -> None:
        path = self._resolved_path()
        if not path:
            return
        with self._lock:
            self._load_locked()
            payload = {
                "format_version": CACHE_FORMAT_VERSION,
                "corpus_fingerprint": self._corpus_fingerprint,
                "embedder_model": self._embedder_model,
                "saved_at": time.time(),
                "entries": [
                    {
                        "scope": e.scope, "query": e.query, "vector": e.vector,
                        "response": e.response, "route": e.route,
                        "corpus_fingerprint": e.corpus_fingerprint,
                        "created_at": e.created_at, "expires_at": e.expires_at,
                        "hits": e.hits,
                    }
                    for e in self._entries
                ],
            }
            self._dirty = False
            self._last_flush = time.time()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as exc:  # noqa: BLE001 — caching must never break a request
            logger.warning("Semantic cache save failed", extra={"error": str(exc)})

    def _maybe_save_locked(self) -> None:
        if self._dirty and (time.time() - self._last_flush) >= self._flush_interval:
            self.save()

    # ── housekeeping ──────────────────────────────────────────────────────────

    def _is_usable(self, entry: CacheEntry, now: float) -> bool:
        if entry.expires_at <= now:
            return False
        # Strict: an answer is only valid for the exact corpus it came from.
        # (Entries are only ever written when a corpus fingerprint is known, so
        # an empty fingerprint here means the entry predates this run.)
        if entry.corpus_fingerprint != self._corpus_fingerprint:
            return False
        return True

    def _prune_stale_locked(self) -> int:
        now = time.time()
        before = len(self._entries)
        self._entries = [e for e in self._entries if self._is_usable(e, now)]
        return before - len(self._entries)

    def _evict_locked(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        # Drop least-recently-created, lowest-hit entries first.
        self._entries.sort(key=lambda e: (e.hits, e.created_at), reverse=True)
        removed = len(self._entries) - self._max_entries
        self._entries = self._entries[: self._max_entries]
        self.stats.evictions += removed

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._dirty = True
        self.save()

    # ── lookup / store ────────────────────────────────────────────────────────

    @staticmethod
    def _scope(employee_id: str, role: str, region: str) -> str:
        return f"{employee_id}|{role}|{region}"

    def is_cacheable_route(self, route: str) -> bool:
        """
        Cache only grounded document answers.

        `tool`/`hybrid` carry live leave data (never cached). `no_answer` is a
        *negative* result that can come from a retrieval miss or a single bad
        model turn — caching it would entrench that failure for the whole TTL
        even after the prompt or model is improved, so it is not reused either.
        """
        return route == "rag"

    def get(self, employee_id: str, role: str, region: str, query: str) -> Optional[dict]:
        """Return a previously stored answer semantically equal to `query`."""
        if not self._enabled:
            return None
        with self._lock:
            self._load_locked()
            self.stats.lookups += 1
            scope = self._scope(employee_id, role, region)
            candidates = [e for e in self._entries if e.scope == scope]
            if not candidates:
                return None

        try:
            import numpy as np
            from src.ingestion.embedder import embed_query

            target = _normalise(query)
            now = time.time()

            # Exact repeat first — no embedding work needed.
            for entry in candidates:
                if entry.query == target and self._is_usable(entry, now):
                    return self._hit(entry, 1.0)

            qvec = embed_query(query)
            with self._lock:
                for entry in candidates:
                    if not self._is_usable(entry, now):
                        continue
                    vec = np.asarray(entry.vector, dtype=np.float32)
                    if vec.shape != qvec.shape:
                        continue
                    sim = float(np.dot(vec, qvec))
                    if sim >= self._threshold:
                        return self._hit(entry, sim)
        except Exception as exc:  # noqa: BLE001 — a cache miss is always safe
            logger.warning("Semantic cache lookup failed", extra={"error": str(exc)})
        return None

    def _hit(self, entry: CacheEntry, similarity: float) -> dict:
        with self._lock:
            entry.hits += 1
            self.stats.hits += 1
            self._dirty = True
            self._maybe_save_locked()
        logger.info(
            "Semantic cache hit",
            extra={"similarity": round(similarity, 4), "query": entry.query[:60], "hits": entry.hits},
        )
        # Deep copy so a caller can never mutate the stored response.
        return json.loads(json.dumps(entry.response))

    def set(self, employee_id: str, role: str, region: str, query: str, response: dict) -> None:
        """Store an answer together with the corpus fingerprint it came from."""
        route = response.get("route", "")
        if not self._enabled or not self.is_cacheable_route(route):
            return
        # Without a corpus fingerprint we cannot guarantee invalidation when the
        # documents change, so we do not store anything at all.
        with self._lock:
            self._load_locked()
            fingerprint = self._corpus_fingerprint
        if not fingerprint:
            return
        try:
            from src.ingestion.embedder import embed_query

            vector = [float(x) for x in embed_query(query)]
        except Exception as exc:  # noqa: BLE001 — caching is best-effort
            logger.warning("Semantic cache store skipped (embedding failed)",
                           extra={"error": str(exc)})
            return

        now = time.time()
        scope = self._scope(employee_id, role, region)
        normalised = _normalise(query)
        with self._lock:
            self._load_locked()
            self._entries = [
                e for e in self._entries
                if not (e.scope == scope and e.query == normalised)
            ]
            self._entries.append(CacheEntry(
                scope=scope, query=normalised, vector=vector, response=response,
                route=route, corpus_fingerprint=fingerprint,
                created_at=now, expires_at=now + self._ttl,
            ))
            self._evict_locked()
            self.stats.stores += 1
            self._dirty = True
            self._maybe_save_locked()

    # ── introspection ─────────────────────────────────────────────────────────

    def stats_snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._load_locked()
            live = [e for e in self._entries if self._is_usable(e, time.time())]
        return {
            "entries": len(live),
            "lookups": self.stats.lookups,
            "hits": self.stats.hits,
            "stores": self.stats.stores,
            "evictions": self.stats.evictions,
            "invalidated_by_fingerprint": self.stats.invalidated_by_fingerprint,
            "corpus_fingerprint": self._corpus_fingerprint[:12],
            "threshold": self._threshold,
            "enabled": self._enabled,
        }


def _build_default_cache() -> SemanticResponseCache:
    """Build the process-wide cache from settings (falls back to safe defaults)."""
    try:
        from src.config import get_settings
        s = get_settings()
        return SemanticResponseCache(
            ttl_seconds=s.cache_ttl_seconds,
            threshold=s.semantic_cache_threshold,
            max_entries=s.semantic_cache_max_entries,
            enabled=s.semantic_cache_enabled,
        )
    except Exception:  # noqa: BLE001
        return SemanticResponseCache()


response_cache = _build_default_cache()

# Backwards-compatible alias (older imports referenced `ResponseCache`).
ResponseCache = SemanticResponseCache
