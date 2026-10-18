"""
src/retrieval/access_filter.py
────────────────────────────────
Pre-LLM access control gate — MUST run before any chunk reaches the LLM.

Filters by:
  1. access_level vs requester role
  2. region match (requester region OR GLOBAL)
  3. effective_date vs reference date (upcoming = separate bucket)
"""
from __future__ import annotations
from datetime import date
from src.security.access_control import can_role_read_doc
from src.utils.logging import get_logger

logger = get_logger(__name__)


def filter_chunks(
    chunks: list[dict],
    requester_role: str,
    requester_region: str,
    reference_date: date,
) -> tuple[list[dict], list[dict]]:
    """
    Split chunks into (eligible, upcoming).

    eligible: chunks the requester can read NOW
    upcoming: chunks with future effective_date that would be eligible when they take effect
              (used to mention upcoming changes)

    Restricted chunks are silently dropped — their existence is NEVER revealed.
    """
    eligible: list[dict] = []
    upcoming: list[dict] = []

    ref_str = reference_date.isoformat()

    for chunk in chunks:
        meta = chunk["metadata"]

        # Gate 1: Role-based document access
        if not can_role_read_doc(requester_role, meta.get("access_level", "all")):
            continue   # Silently drop — never reveal restricted doc exists

        # Gate 2: Region check
        doc_regions = meta.get("regions", ["GLOBAL"])
        if requester_region not in doc_regions and "GLOBAL" not in doc_regions:
            continue

        # Gate 3: Effective date
        eff_date = meta.get("effective_date", "1900-01-01")
        if eff_date > ref_str:
            # Future document — eligible to mention as upcoming change
            upcoming.append(chunk)
        else:
            eligible.append(chunk)

    logger.debug(
        "Access filter applied",
        extra={"eligible": len(eligible), "upcoming": len(upcoming),
               "role": requester_role, "region": requester_region},
    )
    return eligible, upcoming
