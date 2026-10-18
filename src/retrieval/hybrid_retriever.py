"""
src/retrieval/hybrid_retriever.py
───────────────────────────────────
Hybrid retrieval: FAISS dense + BM25 sparse, fused via Reciprocal Rank Fusion (RRF).
Applies confidence threshold before returning — low-score queries return empty list
so the router can route them as no_answer without an LLM call.
"""
from __future__ import annotations

import numpy as np

from src.ingestion.embedder import embed_query, search_faiss
from src.ingestion.ingestor import get_global_index, get_global_bm25
from src.utils.logging import get_logger

logger = get_logger(__name__)

CONFIDENCE_THRESHOLD = 0.30   # Below this best score → no_answer (no LLM call)
RRF_K = 60


def _bm25_search(query: str, chunks: list[dict], top_k: int = 30) -> list[tuple[int, float]]:
    bm25 = get_global_bm25()
    if bm25 is None:
        return []
    try:
        tokenized_query = query.lower().split()
        scores = bm25.get_scores(tokenized_query)
        top_indices = np.argsort(scores)[::-1][:top_k]
        return [(int(i), float(scores[i])) for i in top_indices if scores[i] > 0]
    except Exception as exc:
        logger.warning("BM25 search failed", extra={"error": str(exc)})
        return []


def _rrf_merge(
    dense_results: list[tuple[int, float]],
    sparse_results: list[tuple[int, float]],
    k: int = RRF_K,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion — combines dense + sparse rankings."""
    scores: dict[int, float] = {}
    for rank, (idx, _score) in enumerate(dense_results):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    for rank, (idx, _score) in enumerate(sparse_results):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def retrieve(
    query: str,
    top_k: int = 30,
) -> tuple[list[dict], float]:
    """
    Hybrid retrieval returning (chunks, best_score).
    chunks: top-k candidate chunks (unfiltered — access filter applied next)
    best_score: highest individual relevance score (for confidence threshold check)
    """
    faiss_idx, chunks = get_global_index()
    if faiss_idx is None or not chunks:
        logger.warning("No index available — retrieval skipped")
        return [], 0.0

    # Dense search
    query_vec = embed_query(query)
    dense_results = search_faiss(faiss_idx, query_vec, top_k=top_k)

    # Sparse search
    sparse_results = _bm25_search(query, chunks, top_k=top_k)

    # RRF fusion
    fused = _rrf_merge(dense_results, sparse_results)

    # Build result chunks
    best_dense_score = dense_results[0][1] if dense_results else 0.0
    result_chunks = []
    for chunk_idx, _rrf_score in fused[:top_k]:
        if chunk_idx < len(chunks):
            result_chunks.append(chunks[chunk_idx])

    logger.debug(
        "Hybrid retrieval",
        extra={"query_preview": query[:60], "results": len(result_chunks), "best_score": round(best_dense_score, 3)},
    )
    return result_chunks, best_dense_score


def is_below_confidence_threshold(best_score: float, threshold: float = CONFIDENCE_THRESHOLD) -> bool:
    """True if the best relevance score is too low to trust."""
    return best_score < threshold
