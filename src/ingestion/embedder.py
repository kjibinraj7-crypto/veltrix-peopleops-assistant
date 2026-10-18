"""
src/ingestion/embedder.py
──────────────────────────
BGE-small-en-v1.5 embedding generation + FAISS + BM25 index management.

CRITICAL — BGE query prefix rule:
    Documents: encoded WITHOUT prefix
    Queries:   encoded WITH prefix "Represent this sentence for searching relevant passages: "
    Forgetting this degrades retrieval quality significantly.
"""
from __future__ import annotations

import json
import os
import numpy as np
from src.utils.logging import get_logger

logger = get_logger(__name__)

BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Lazy globals — loaded once per process
_model = None
_faiss_index = None
_chunks_metadata: list[dict] = []


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model", extra={"model": MODEL_NAME})
        _model = SentenceTransformer(MODEL_NAME)
        logger.info("Embedding model loaded")
    return _model


def embed_documents(texts: list[str]) -> np.ndarray:
    """Embed document texts (NO prefix)."""
    model = get_model()
    return model.encode(texts, batch_size=32, show_progress_bar=False, normalize_embeddings=True)


def embed_query(query: str) -> np.ndarray:
    """Embed a search query (WITH BGE prefix)."""
    model = get_model()
    prefixed = BGE_QUERY_PREFIX + query
    vec = model.encode([prefixed], normalize_embeddings=True)
    return vec[0]


def build_faiss_index(embeddings: np.ndarray) -> object:
    """Build a FAISS flat inner-product index (cosine sim since vectors are normalized)."""
    import faiss
    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings.astype(np.float32))
    logger.info("FAISS index built", extra={"vectors": index.ntotal, "dim": dim})
    return index


def save_index(vector_store_dir: str, faiss_index, chunks: list[dict]) -> None:
    import faiss
    os.makedirs(vector_store_dir, exist_ok=True)
    faiss.write_index(faiss_index, os.path.join(vector_store_dir, "index.faiss"))
    with open(os.path.join(vector_store_dir, "chunks.json"), "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)
    logger.info("FAISS index saved", extra={"chunks": len(chunks)})


def load_index(vector_store_dir: str) -> tuple[object, list[dict]]:
    import faiss
    idx = faiss.read_index(os.path.join(vector_store_dir, "index.faiss"))
    with open(os.path.join(vector_store_dir, "chunks.json"), encoding="utf-8") as f:
        chunks = json.load(f)
    logger.info("FAISS index loaded", extra={"chunks": len(chunks)})
    return idx, chunks


def search_faiss(faiss_index, query_vec: np.ndarray, top_k: int = 30) -> list[tuple[int, float]]:
    """Return list of (chunk_index, score) ordered by score desc."""
    scores, indices = faiss_index.search(
        query_vec.reshape(1, -1).astype(np.float32), top_k
    )
    return [(int(i), float(s)) for i, s in zip(indices[0], scores[0]) if i >= 0]
