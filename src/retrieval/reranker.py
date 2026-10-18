"""
src/retrieval/reranker.py
──────────────────────────
Cross-encoder reranking using flashrank.
Falls back gracefully if flashrank is not installed.
"""
from __future__ import annotations

from src.utils.logging import get_logger

logger = get_logger(__name__)

_ranker = None
_reranker_available = None


def _get_ranker():
    global _ranker, _reranker_available
    if _reranker_available is None:
        try:
            from flashrank import Ranker
            _ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir="/tmp/flashrank")
            _reranker_available = True
            logger.info("Reranker loaded (flashrank ms-marco-MiniLM-L-12-v2)")
        except Exception as exc:
            _reranker_available = False
            logger.warning("Reranker not available — using original order", extra={"error": str(exc)})
    return _ranker if _reranker_available else None


def rerank(query: str, chunks: list[dict], top_k: int | None = None) -> list[dict]:
    """
    Rerank chunks using cross-encoder scoring.
    Returns chunks in reranked order (best first).
    Falls back to input order if flashrank is unavailable.
    """
    if not chunks:
        return chunks

    ranker = _get_ranker()
    if ranker is None:
        return chunks

    try:
        from flashrank import RerankRequest
        passages = [{"id": i, "text": c["text"]} for i, c in enumerate(chunks)]
        request = RerankRequest(query=query, passages=passages)
        results = ranker.rerank(request)
        # results is list of dicts with 'id' and 'score'
        reranked = [chunks[r["id"]] for r in results if r["id"] < len(chunks)]
        if top_k:
            reranked = reranked[:top_k]
        logger.debug("Reranking applied", extra={"chunks_in": len(chunks), "chunks_out": len(reranked)})
        return reranked
    except Exception as exc:
        logger.warning("Reranking failed — using original order", extra={"error": str(exc)})
        return chunks
