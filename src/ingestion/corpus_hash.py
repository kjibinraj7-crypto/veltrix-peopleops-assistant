"""
src/ingestion/corpus_hash.py
─────────────────────────────
Hash-based corpus change detection.
If corpus is unchanged since last run, reuse the saved FAISS index.
"""
from __future__ import annotations
import hashlib
import json
import os
from src.utils.logging import get_logger

logger = get_logger(__name__)
HASH_FILENAME = "corpus.hash"


def compute_corpus_hash(corpus_dir: str, manifest_path: str) -> str:
    """
    Hash the corpus *contents* (file names + bytes) plus the manifest.

    Contents — not just size and mtime — are hashed so that a same-size edit, or
    a corpus copied in a way that preserves timestamps, is still detected and
    triggers a full rebuild.
    """
    h = hashlib.sha256()
    for fname in sorted(os.listdir(corpus_dir)):
        fpath = os.path.join(corpus_dir, fname)
        if os.path.isfile(fpath):
            h.update(fname.encode())
            h.update(str(os.path.getsize(fpath)).encode())
            with open(fpath, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
    if os.path.isfile(manifest_path):
        h.update(open(manifest_path, "rb").read())
    return h.hexdigest()


def load_saved_hash(vector_store_dir: str) -> str | None:
    path = os.path.join(vector_store_dir, HASH_FILENAME)
    if os.path.isfile(path):
        return open(path).read().strip()
    return None


def save_hash(vector_store_dir: str, hash_value: str) -> None:
    os.makedirs(vector_store_dir, exist_ok=True)
    path = os.path.join(vector_store_dir, HASH_FILENAME)
    with open(path, "w") as f:
        f.write(hash_value)
    logger.info("Corpus hash saved", extra={"hash": hash_value[:12]})


def corpus_changed(corpus_dir: str, manifest_path: str, vector_store_dir: str) -> bool:
    current = compute_corpus_hash(corpus_dir, manifest_path)
    saved = load_saved_hash(vector_store_dir)
    changed = current != saved
    logger.info("Corpus change check", extra={"changed": changed})
    return changed, current
