"""
src/ingestion/ingestor.py
──────────────────────────
Orchestrates the full ingestion pipeline. Called at startup.

Pipeline:
  1. Check corpus hash → skip rebuild if unchanged
  2. Load manifest.json
  3. For each document: parse → clean → chunk
  4. Extract holidays from calendar docs → SQLite
  5. Build FAISS + BM25 indexes from all chunks
  6. Save indexes + hash
  7. Update ingestion_state (used by /health endpoint)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import numpy as np

from src.config import get_settings
from src.ingestion.chunker import chunk_sections, chunk_pages
from src.ingestion.corpus_hash import corpus_changed, save_hash, compute_corpus_hash
from src.ingestion.embedder import embed_documents, build_faiss_index, save_index, load_index, get_model
from src.ingestion.holiday_extractor import extract_holidays_from_document, ensure_holidays_table
from src.ingestion.integrity import (
    IngestionReport,
    classify_document,
    classify_page,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


def _load_manifest(manifest_path: str) -> list[dict]:
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get("documents", [])


def _is_within(root: str, path: str) -> bool:
    """True when `path` resolves to the corpus root itself or something inside it."""
    resolved = os.path.realpath(path)
    return resolved == root or resolved.startswith(root + os.sep)


def _load_saved_integrity(vector_store_dir: str, corpus_hash: str = "") -> dict | None:
    """
    Read the integrity summary from a previous run, but only if it describes the
    *same* corpus. Returns None when the file is missing or stale, so the caller
    can regenerate it.
    """
    path = os.path.join(vector_store_dir, "ingestion_report.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if corpus_hash and data.get("corpus_hash") != corpus_hash:
        return None
    return data.get("summary")


def _parse_all(
    manifest: list[dict],
    corpus_dir: str,
    report: IngestionReport,
    settings: Any,
    *,
    extract_holidays: bool,
) -> list[dict]:
    """
    Parse and chunk every document in the manifest, recording integrity findings.

    `extract_holidays` is True only on a full ingest — the audit-only pass does
    not touch the database.
    """
    all_chunks: list[dict] = []

    corpus_root = os.path.realpath(corpus_dir)

    for doc_meta in manifest:
        file_name = doc_meta.get("file", "")
        file_path = os.path.join(corpus_dir, file_name)

        # Defence-in-depth: the manifest is deployment data, but a filename that
        # escapes the corpus directory ("../secrets.txt", an absolute path, ...)
        # must never be parsed or have holidays extracted from it.
        if not _is_within(corpus_root, file_path):
            logger.warning("Manifest entry escapes the corpus directory",
                           extra={"file": file_name})
            report.add_missing_file(file_name)
            continue

        if not os.path.isfile(file_path):
            logger.warning("File not found", extra={"file": file_path})
            report.add_missing_file(file_name)
            continue

        # Extract holidays from calendar documents
        if extract_holidays and doc_meta.get("doc_type") == "calendar":
            try:
                n = extract_holidays_from_document(file_path, doc_meta)
                logger.info("Holidays extracted", extra={"file": file_name, "count": n})
            except Exception as exc:
                logger.warning("Holiday extraction failed", extra={"file": file_name, "error": str(exc)})

        # Parse and chunk (records per-page integrity findings in `report`)
        chunks = _parse_document(
            file_path, doc_meta, report=report,
            min_page_chars=settings.min_page_chars,
        )
        all_chunks.extend(chunks)

    return all_chunks


def _audit_corpus(corpus_dir: str, manifest_path: str, settings: Any) -> IngestionReport:
    """
    Integrity-only pass: parse and chunk the whole corpus, no embeddings.

    Used when we serve a cached index but have no report for the current corpus,
    so "was anything missed?" is always answerable.
    """
    report = IngestionReport()
    try:
        manifest = _load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        report.add_parse_error("manifest.json", str(exc))
        return report
    _parse_all(manifest, corpus_dir, report, settings, extract_holidays=False)
    return report


def _ensure_integrity(
    vector_store_dir: str,
    corpus_dir: str,
    manifest_path: str,
    corpus_hash: str,
    settings: Any,
) -> dict:
    """
    Guarantee an integrity report exists for the corpus currently being served.

    Reuses the saved report when it matches the corpus hash (no repeated work on
    restart); regenerates it when the corpus has changed or no report exists.
    """
    saved = _load_saved_integrity(vector_store_dir, corpus_hash)
    if saved is not None:
        logger.info("Ingestion integrity report reused (corpus unchanged)", extra=saved)
        return saved

    logger.info("No integrity report for the current corpus — auditing now")
    report = _audit_corpus(corpus_dir, manifest_path, settings)
    report.print_report()
    report.save(vector_store_dir, corpus_hash=corpus_hash)
    return report.summary()


def _parse_document(
    file_path: str,
    doc_meta: dict,
    report: IngestionReport | None = None,
    min_page_chars: int = 0,
) -> list[dict]:
    """
    Parse one document and return its chunks.

    When `report` is given, every PDF page is classified (ok / OCR-recovered /
    flagged) and the document itself is checked for thin or empty extraction.
    """
    ext = file_path.lower().split(".")[-1]
    file_name = os.path.basename(file_path)
    doc_id = doc_meta.get("doc_id", file_name)
    chunks: list[dict] = []
    total_chars = 0

    try:
        if ext == "pdf":
            from src.ingestion.parsers.pdf_parser import parse_pdf
            parsed = parse_pdf(file_path, min_page_chars=min_page_chars)
            for finding in parsed.page_findings:
                if report is not None:
                    report.add_page(classify_page(
                        file_name=file_name, doc_id=doc_id,
                        page_number=finding["page"],
                        chars_extracted=finding["chars_extracted"],
                        chars_final=finding["chars_final"],
                        used_ocr=finding["used_ocr"],
                        min_page_chars=min_page_chars,
                    ))
            total_chars = sum(len(p.text) for p in parsed.pages)
            chunks = chunk_pages(parsed.pages, doc_meta)

        elif ext in ("doc", "docx"):
            from src.ingestion.parsers.docx_parser import parse_docx
            parsed = parse_docx(file_path)
            total_chars = sum(len(s.get("text", "")) for s in parsed.sections)
            chunks = chunk_sections(parsed.sections, doc_meta)

        elif ext in ("html", "htm"):
            from src.ingestion.parsers.html_parser import parse_html
            parsed = parse_html(file_path)
            total_chars = sum(len(s.get("text", "")) for s in parsed.sections)
            chunks = chunk_sections(parsed.sections, doc_meta)

        elif ext == "md":
            from src.ingestion.parsers.markdown_parser import parse_markdown
            parsed = parse_markdown(file_path)
            total_chars = sum(len(s.get("text", "")) for s in parsed.sections)
            chunks = chunk_sections(parsed.sections, doc_meta)

        else:
            logger.warning("Unknown file extension", extra={"file": file_path, "ext": ext})

    except Exception as exc:
        logger.error("Document parse failed", extra={"file": file_path, "error": str(exc)})
        if report is not None:
            report.add_parse_error(file_name, str(exc))

    if report is not None:
        total_chars = total_chars or sum(len(c["text"]) for c in chunks)
        report.add_document(classify_document(
            file_name=file_name, doc_id=doc_id, total_chars=total_chars,
            chunks=len(chunks), min_document_chars=get_settings().min_document_chars,
        ))

    logger.info("Document parsed", extra={"file": file_name, "chunks": len(chunks)})
    return chunks


def run_ingestion(ingestion_state: dict[str, Any]) -> None:
    """
    Full ingestion pipeline. Runs synchronously at startup.
    Updates ingestion_state in-place — /health polls this dict.
    """
    settings = get_settings()
    corpus_dir = settings.corpus_dir
    manifest_path = settings.manifest_path
    vector_store_dir = settings.vector_store_dir

    t0 = time.time()
    logger.info("Ingestion starting", extra={"corpus_dir": corpus_dir})

    # ── Step 1: Check if rebuild needed ──────────────────────────────────────
    changed, current_hash = corpus_changed(corpus_dir, manifest_path, vector_store_dir)

    # Tell the semantic cache which corpus produced the answers it holds. If the
    # hash differs from the one stamped on a stored answer, that answer is never
    # reused (documents were updated → old responses are stale).
    try:
        from src.ingestion.embedder import MODEL_NAME
        from src.orchestration.cache import response_cache
        response_cache.set_corpus_fingerprint(current_hash, embedder_model=MODEL_NAME)
    except Exception as exc:  # noqa: BLE001 — never block startup on cache bookkeeping
        logger.warning("Could not update semantic cache fingerprint", extra={"error": str(exc)})

    faiss_path = os.path.join(vector_store_dir, "index.faiss")
    chunks_path = os.path.join(vector_store_dir, "chunks.json")
    bm25_path = os.path.join(vector_store_dir, "bm25_index.json")

    if not changed and os.path.isfile(faiss_path) and os.path.isfile(chunks_path):
        logger.info("Corpus unchanged — loading existing index")
        try:
            faiss_idx, chunks = load_index(vector_store_dir)
            _set_global_index(faiss_idx, chunks)
            _load_bm25(bm25_path, chunks)
            ingestion_state.update({
                "ready": True,
                "docs_ingested": len(set(c["metadata"]["doc_id"] for c in chunks)),
                "chunks": len(chunks),
                "finished_at": time.time(),
            })
            # A cached index must still be able to answer "was any page missed?".
            # Reuses the saved report for this corpus, or regenerates it once.
            ingestion_state["integrity"] = _ensure_integrity(
                vector_store_dir, corpus_dir, manifest_path, current_hash, settings
            )
            logger.info("Ingestion complete (cached)", extra={"chunks": len(chunks)})
            return
        except Exception as exc:
            logger.warning("Cached index load failed, rebuilding", extra={"error": str(exc)})

    # ── Step 2: Load manifest ─────────────────────────────────────────────────
    try:
        manifest = _load_manifest(manifest_path)
    except Exception as exc:
        logger.error("Manifest load failed", extra={"error": str(exc)})
        ingestion_state["error"] = f"Manifest load failed: {exc}"
        return

    logger.info("Manifest loaded", extra={"documents": len(manifest)})

    # ── Step 3: Ensure holidays table ─────────────────────────────────────────
    ensure_holidays_table()

    # ── Step 4: Parse all documents (with integrity checks) ───────────────────
    report = IngestionReport()
    all_chunks = _parse_all(
        manifest, corpus_dir, report, settings, extract_holidays=True
    )

    logger.info("All documents parsed", extra={"total_chunks": len(all_chunks)})

    # ── Step 4b: Integrity report — print BEFORE embeddings are built ─────────
    # Any page with thin/empty extraction, any document with too little text or
    # no chunks, and any missing file is listed here so it cannot go unnoticed.
    report.print_report()
    report.save(vector_store_dir, corpus_hash=current_hash)
    ingestion_state["integrity"] = report.summary()

    if not all_chunks:
        ingestion_state["error"] = "No chunks produced — check corpus directory"
        return

    # ── Step 5: Build embeddings + FAISS ─────────────────────────────────────
    logger.info("Building embeddings...")
    texts = [c["text"] for c in all_chunks]
    embeddings = embed_documents(texts)
    faiss_idx = build_faiss_index(embeddings)

    # ── Step 6: Build BM25 ───────────────────────────────────────────────────
    logger.info("Building BM25 index...")
    _build_and_save_bm25(bm25_path, all_chunks)

    # ── Step 7: Save everything ───────────────────────────────────────────────
    save_index(vector_store_dir, faiss_idx, all_chunks)
    save_hash(vector_store_dir, current_hash)
    _set_global_index(faiss_idx, all_chunks)

    elapsed = round(time.time() - t0, 1)
    docs_count = len(set(c["metadata"]["doc_id"] for c in all_chunks))
    ingestion_state.update({
        "ready": True,
        "docs_ingested": docs_count,
        "chunks": len(all_chunks),
        "finished_at": time.time(),
    })
    logger.info(
        "Ingestion complete",
        extra={"docs": docs_count, "chunks": len(all_chunks), "elapsed_seconds": elapsed},
    )


# ─── Global index references (read by retrieval layer) ───────────────────────

_global_faiss_index = None
_global_chunks: list[dict] = []
_global_bm25 = None


def _set_global_index(faiss_idx, chunks: list[dict]) -> None:
    global _global_faiss_index, _global_chunks
    _global_faiss_index = faiss_idx
    _global_chunks = chunks


def get_global_index():
    return _global_faiss_index, _global_chunks


def get_global_bm25():
    return _global_bm25


def _build_and_save_bm25(bm25_path: str, chunks: list[dict]) -> None:
    global _global_bm25
    try:
        from rank_bm25 import BM25Okapi
        tokenized = [c["text"].lower().split() for c in chunks]
        _global_bm25 = BM25Okapi(tokenized)
        # Save tokenized corpus for reload
        os.makedirs(os.path.dirname(bm25_path) if os.path.dirname(bm25_path) else ".", exist_ok=True)
        with open(bm25_path, "w", encoding="utf-8") as f:
            json.dump({"tokenized": tokenized}, f)
        logger.info("BM25 index built and saved")
    except Exception as exc:
        logger.warning("BM25 build failed", extra={"error": str(exc)})


def _load_bm25(bm25_path: str, chunks: list[dict]) -> None:
    global _global_bm25
    try:
        from rank_bm25 import BM25Okapi
        if os.path.isfile(bm25_path):
            with open(bm25_path, encoding="utf-8") as f:
                data = json.load(f)
            _global_bm25 = BM25Okapi(data["tokenized"])
        else:
            tokenized = [c["text"].lower().split() for c in chunks]
            _global_bm25 = BM25Okapi(tokenized)
        logger.info("BM25 index loaded")
    except Exception as exc:
        logger.warning("BM25 load failed", extra={"error": str(exc)})
