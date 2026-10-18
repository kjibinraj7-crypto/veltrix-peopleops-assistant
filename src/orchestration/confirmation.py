"""
src/orchestration/confirmation.py
───────────────────────────────────
Two-step confirmation flow for apply_leave and cancel_leave.

Rules (task §8.2):
- Step 1: Validate + propose → pending_confirmation=true, no DB write
- Step 2: User confirms → execute → status=success/error
- Any other message → discard proposal, handle normally
- NEVER execute without confirmation even if user says "skip it"
"""
from __future__ import annotations

import re

# Exact-match confirmation phrases (normalized: lowercased, stripped, trailing
# punctuation removed). Deliberately strict — only unambiguous confirmations.
_CONFIRM_PHRASES = frozenset({
    "yes", "yes please", "yep", "yeah", "confirm", "confirmed", "go ahead",
    "proceed", "do it", "ok", "okay", "sure", "approve", "submit",
})

# Exact-match, unambiguous cancellation phrases. A message that merely *contains*
# a change ("no, make it Friday") is NOT treated as a cancellation — the router
# discards the pending proposal and handles the new message normally so it can
# produce a fresh proposal.
_CANCEL_PHRASES = frozenset({
    "no", "nope", "nah", "no thanks", "no thank you", "cancel", "cancel it",
    "cancelled", "never mind", "nevermind", "forget it", "forget about it",
    "stop", "abort", "discard", "discard it", "don't", "do not", "leave it",
})


def _normalize(message: str) -> str:
    return re.sub(r"\s+", " ", message.lower().strip().rstrip("!."))


def is_confirmation(message: str) -> bool:
    """Check if the user's message is a clear, unambiguous confirmation."""
    return _normalize(message) in _CONFIRM_PHRASES


def is_cancellation(message: str) -> bool:
    """Check if the user is explicitly cancelling the pending proposal."""
    if not message:
        return False
    return _normalize(message) in _CANCEL_PHRASES
