"""
src/ingestion/chunker.py
─────────────────────────
Section-aware chunker. Each chunk carries full metadata for retrieval.

Chunk structure:
    {
        "chunk_id": "uuid",
        "text": "...",
        "token_count": 342,
        "metadata": {
            "doc_id": "<DOC-ID>",
            "title": "<Document title>",
            "doc_type": "policy",
            "regions": ["<REGION>"],
            "effective_date": "YYYY-MM-DD",
            "access_level": "all",
            "section_heading": "<Section heading>",
            "page_number": 1,
            "file": "<source file name>"
        }
    }
"""
from __future__ import annotations
import uuid
from typing import Any

import tiktoken

from src.utils.logging import get_logger

logger = get_logger(__name__)

_enc = tiktoken.get_encoding("cl100k_base")

TARGET_CHUNK_TOKENS = 400    # Target tokens per chunk
OVERLAP_TOKENS = 50          # Token overlap between consecutive chunks
MAX_CHUNK_TOKENS = 500       # Hard cap


def _count_tokens(text: str) -> int:
    return len(_enc.encode(text))


def _split_text_with_overlap(text: str, target: int = TARGET_CHUNK_TOKENS, overlap: int = OVERLAP_TOKENS) -> list[str]:
    """Split a long text into chunks of ~target tokens with overlap."""
    tokens = _enc.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + target, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = _enc.decode(chunk_tokens)
        if chunk_text.strip():
            chunks.append(chunk_text)
        if end >= len(tokens):
            break
        start = end - overlap
    return chunks if chunks else [text]


def chunk_sections(
    sections: list[dict],
    doc_metadata: dict[str, Any],
) -> list[dict]:
    """
    Convert parsed document sections into chunks with metadata.
    Each section already has: heading, text, is_table.
    Tables are kept as single chunks (never split).
    Long text sections are split with overlap.
    """
    chunks: list[dict] = []

    for section in sections:
        heading = section.get("heading", "")
        text = section.get("text", "").strip()
        is_table = section.get("is_table", False)
        page_num = section.get("page_number")

        if not text:
            continue

        # Add heading as context prefix if present
        chunk_text = f"{heading}\n\n{text}" if heading else text

        if is_table or _count_tokens(chunk_text) <= MAX_CHUNK_TOKENS:
            # Tables and small sections → single chunk
            chunks.append(_make_chunk(chunk_text, heading, page_num, doc_metadata))
        else:
            # Large sections → split with overlap
            sub_texts = _split_text_with_overlap(text)
            for sub in sub_texts:
                sub_with_heading = f"{heading}\n\n{sub}" if heading else sub
                chunks.append(_make_chunk(sub_with_heading, heading, page_num, doc_metadata))

    return chunks


def chunk_pages(
    pages: list[Any],  # ParsedPage objects (PDF)
    doc_metadata: dict[str, Any],
) -> list[dict]:
    """
    Convert PDF pages into chunks. Each page becomes one or more chunks.
    """
    chunks: list[dict] = []
    for page in pages:
        text = page.text.strip()
        if not text:
            continue
        heading = page.headings[0] if page.headings else ""
        page_num = page.page_number

        chunk_text = f"{heading}\n\n{text}" if heading else text
        if _count_tokens(chunk_text) <= MAX_CHUNK_TOKENS:
            chunks.append(_make_chunk(chunk_text, heading, page_num, doc_metadata))
        else:
            for sub in _split_text_with_overlap(text):
                sub_text = f"{heading}\n\n{sub}" if heading else sub
                chunks.append(_make_chunk(sub_text, heading, page_num, doc_metadata))

    return chunks


def _make_chunk(text: str, heading: str, page_number: Any, doc_metadata: dict) -> dict:
    token_count = _count_tokens(text)
    return {
        "chunk_id": str(uuid.uuid4()),
        "text": text,
        "token_count": token_count,
        "metadata": {
            "doc_id":          doc_metadata["doc_id"],
            "title":           doc_metadata.get("title", ""),
            "doc_type":        doc_metadata.get("doc_type", ""),
            "regions":         doc_metadata.get("regions", ["GLOBAL"]),
            "effective_date":  doc_metadata.get("effective_date", "1900-01-01"),
            "access_level":    doc_metadata.get("access_level", "all"),
            "section_heading": heading,
            "page_number":     page_number,
            "file":            doc_metadata.get("file", ""),
        },
    }
