"""
src/retrieval/token_budget.py
──────────────────────────────
Token budget enforcement for corpus text sent to the LLM.

Rules (task §4.3):
- Maximum 3,000 tokens of corpus text per LLM call
- Always use tiktoken cl100k_base regardless of LLM provider
- Log the token count for every LLM call
"""
from __future__ import annotations

import tiktoken

from src.utils.logging import get_logger

logger = get_logger(__name__)

_enc = tiktoken.get_encoding("cl100k_base")
DEFAULT_BUDGET = 3000


def count_tokens(text: str) -> int:
    """Count tokens using cl100k_base (always, regardless of LLM provider)."""
    return len(_enc.encode(text))


def _neutralise_doc_tags(text: str) -> str:
    """
    Stop document text from breaking out of its own <DOC> delimiter.

    Without this, a document containing a literal ``</DOC>`` could close the
    reference-data block early, so anything after it would reach the model
    *outside* the region the system prompt declares to be untrusted data.
    """
    return text.replace("<DOC", "&lt;DOC").replace("</DOC>", "&lt;/DOC&gt;")


def _format_chunk_block(chunk: dict) -> str:
    """Wrap a single chunk in its <DOC> delimiter block."""
    meta = chunk["metadata"]
    doc_id = meta.get("doc_id", "UNKNOWN")
    heading = meta.get("section_heading", "")
    eff_date = meta.get("effective_date", "")
    doc_type = meta.get("doc_type", "")

    header = f'<DOC id="{doc_id}" type="{doc_type}" effective="{eff_date}"'
    if heading:
        header += f' section="{heading}"'
    header += ">"

    return f"{header}\n{_neutralise_doc_tags(chunk['text'])}\n</DOC>"


def select_chunks_within_budget(
    chunks: list[dict],
    budget: int = DEFAULT_BUDGET,
    request_id: str = "",
) -> list[dict]:
    """
    Greedily include chunks (already sorted by relevance) until token budget is
    exhausted. The budget counts the *formatted* <DOC> block (including the tag
    overhead), so the total corpus text sent to the LLM never exceeds `budget`.
    Logs the final token count for every call.
    """
    selected: list[dict] = []
    blocks: list[str] = []
    total_tokens = 0
    separator_tokens = count_tokens("\n\n")

    for chunk in chunks:
        block = _format_chunk_block(chunk)
        block_tokens = count_tokens(block)
        extra = block_tokens + (separator_tokens if blocks else 0)
        if total_tokens + extra > budget:
            break
        blocks.append(block)
        selected.append(chunk)
        total_tokens += extra

    logger.info(
        "Token budget applied",
        extra={
            "request_id":       request_id,
            "corpus_tokens":    total_tokens,
            "budget":           budget,
            "chunks_selected":  len(selected),
            "chunks_available": len(chunks),
        },
    )
    return selected


def format_chunks_for_prompt(chunks: list[dict]) -> str:
    """
    Format selected chunks as delimited context for the LLM prompt.
    Each chunk is wrapped in <DOC> tags to enable prompt injection defense.
    """
    return "\n\n".join(_format_chunk_block(chunk) for chunk in chunks)


def format_upcoming_for_prompt(upcoming_chunks: list[dict]) -> str:
    """Format upcoming-change chunks for the LLM prompt."""
    if not upcoming_chunks:
        return ""
    parts = ["<UPCOMING_CHANGES>"]
    for chunk in upcoming_chunks[:3]:  # Limit to 3 upcoming docs
        meta = chunk["metadata"]
        parts.append(
            f'  - {meta.get("doc_id")}: effective {meta.get("effective_date")} — '
            f'{chunk["text"][:200]}...'
        )
    parts.append("</UPCOMING_CHANGES>")
    return "\n".join(parts)
