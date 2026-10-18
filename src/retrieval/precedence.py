"""
src/retrieval/precedence.py
────────────────────────────
Document precedence engine. Resolves conflicts when multiple eligible documents
give different values for the same policy rule.

Three-tier precedence:
  1. Authority:  policy|addendum|memo|handbook|calendar  >  faq|wiki
  2. Scope:      region-specific  >  GLOBAL
  3. Recency:    later effective_date wins

If still tied → surface both values + both citations + conflict note.
"""
from __future__ import annotations

from src.utils.logging import get_logger

logger = get_logger(__name__)

# Authority ranking (higher = more authoritative).
# Task §5.1: policy, addendum, memo, handbook and calendar are ONE authority tier
# that outranks faq and wiki. Anything unrecognised falls below faq+wiki as a
# defensive default rather than silently ranking equal to a policy.
_AUTHORITY: dict[str, int] = {
    "policy":    5,
    "addendum":  5,
    "handbook":  5,
    "memo":      5,
    "calendar":  5,
    "faq":       2,
    "wiki":      1,
    "other":     0,
}


def authority_rank(doc_type: str) -> int:
    return _AUTHORITY.get(doc_type.lower(), 0)


def scope_rank(regions: list[str], requester_region: str) -> int:
    """Region-specific > GLOBAL."""
    if requester_region in regions:
        return 1   # Specific match
    return 0       # GLOBAL fallback


def sort_chunks_by_precedence(
    chunks: list[dict],
    requester_region: str,
) -> list[dict]:
    """
    Sort eligible chunks by precedence (most authoritative first).
    Used by the token budget step to prioritise which chunks enter the prompt.
    """
    def _key(chunk: dict):
        meta = chunk["metadata"]
        return (
            authority_rank(meta.get("doc_type", "")),          # Higher authority first
            scope_rank(meta.get("regions", []), requester_region),  # Region-specific first
            meta.get("effective_date", "1900-01-01"),           # Later date first
        )

    return sorted(chunks, key=_key, reverse=True)


def get_conflict_note(chunks: list[dict]) -> str | None:
    """
    Check if multiple high-authority chunks disagree on a topic.
    Returns a conflict note string if conflicts exist, else None.
    Used by the router to add a warning in the LLM prompt.
    """
    if len(chunks) <= 1:
        return None
    doc_types = [c["metadata"].get("doc_type", "") for c in chunks[:5]]
    # Simple heuristic: if there are multiple policy/faq docs, warn about potential conflict
    policy_count = sum(1 for dt in doc_types if dt in ("policy", "addendum"))
    faq_count = sum(1 for dt in doc_types if dt in ("faq", "wiki"))
    if policy_count >= 1 and faq_count >= 1:
        return (
            "Note: The retrieved documents contain both policy documents and FAQ/wiki entries "
            "that may give different values for the same rule. "
            "Apply the precedence rule: policy/addendum > faq/wiki > wiki. "
            "State the winning value and cite both sources."
        )
    return None
