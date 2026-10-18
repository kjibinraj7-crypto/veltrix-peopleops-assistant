"""
src/orchestration/session.py
─────────────────────────────
In-memory session management with security enforcement.

Rules (task §7.1 and §8.2):
- A session belongs to the employee who sent its first message.
- Any later request with the same session_id but a different X-Employee-Id → HTTP 403.
- Sessions store: owner employee_id, conversation history, pending confirmation.
- In-memory storage is acceptable for a single-container deployment.
- One pending confirmation per session at most (task §8.2).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import HTTPException

from src.utils.logging import get_logger

logger = get_logger(__name__)

# ─── Session data model ───────────────────────────────────────────────────────

@dataclass
class PendingConfirmation:
    """A write action that has been proposed but not yet confirmed."""
    tool_name: str                  # 'apply_leave' | 'cancel_leave'
    tool_args: dict[str, Any]       # The validated, ready-to-execute args
    summary: str                    # Human-readable summary shown to the user
    proposal_answer: str            # Full answer text from the proposal step


@dataclass
class Session:
    """State for a single conversation session."""
    session_id: str
    employee_id: str                # Owner — validated on every request
    history: list[dict[str, str]] = field(default_factory=list)
    pending_confirmation: Optional[PendingConfirmation] = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


# ─── Session store ────────────────────────────────────────────────────────────

class SessionStore:
    """
    Thread-safe in-memory session store.
    Key: session_id → Session
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        # Opportunistic expiry: without this, every invented session_id would be
        # kept forever and the store would grow without bound.
        self._cleanup_interval = 300.0
        self._last_cleanup = time.time()

    def _maybe_cleanup(self) -> None:
        """Drop idle sessions at most once per interval (never under the lock)."""
        if time.time() - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = time.time()
        try:
            self.cleanup_stale()
        except Exception as exc:  # noqa: BLE001 — housekeeping must never fail a request
            logger.warning("Session cleanup failed", extra={"error": str(exc)})

    def get_or_create(self, session_id: str, employee_id: str) -> Session:
        """
        Return an existing session or create a new one.

        Security rule: If the session already exists and belongs to a DIFFERENT
        employee, raise HTTP 403 immediately. Do not expose session data.
        """
        self._maybe_cleanup()
        with self._lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                if session.employee_id != employee_id:
                    logger.warning(
                        "Session hijack attempt blocked",
                        extra={
                            "session_id":       session_id,
                            "owner_id":         session.employee_id,
                            "requester_id":     employee_id,
                        },
                    )
                    raise HTTPException(
                        status_code=403,
                        detail="This session belongs to another user.",
                    )
                session.last_active = time.time()
                return session

            # New session — create and register
            session = Session(session_id=session_id, employee_id=employee_id)
            self._sessions[session_id] = session
            logger.info(
                "Session created",
                extra={"session_id": session_id, "employee_id": employee_id},
            )
            return session

    def add_message(
        self,
        session: Session,
        role: str,
        content: str,
    ) -> None:
        """Append a message to the session's conversation history."""
        with self._lock:
            session.history.append({"role": role, "content": content})
            session.last_active = time.time()

    def set_pending_confirmation(
        self,
        session: Session,
        confirmation: Optional[PendingConfirmation],
    ) -> None:
        """Set or clear the pending confirmation for this session."""
        with self._lock:
            session.pending_confirmation = confirmation
            session.last_active = time.time()

    def clear_pending_confirmation(self, session: Session) -> None:
        """Discard any pending confirmation (e.g. user sent a different message)."""
        with self._lock:
            session.pending_confirmation = None

    def get_history(self, session: Session) -> list[dict[str, str]]:
        """Return a copy of the conversation history (safe to pass to LLM)."""
        with self._lock:
            return list(session.history)

    def delete(self, session_id: str) -> None:
        """Remove a session from the store."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def cleanup_stale(self, max_age_seconds: int = 3600) -> int:
        """
        Remove sessions idle for longer than max_age_seconds.
        Returns the number of sessions removed.
        Called periodically to prevent unbounded memory growth.
        """
        cutoff = time.time() - max_age_seconds
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s.last_active < cutoff]
            for sid in stale:
                del self._sessions[sid]
        if stale:
            logger.info("Stale sessions cleaned up", extra={"count": len(stale)})
        return len(stale)

    @property
    def session_count(self) -> int:
        """Current number of active sessions."""
        with self._lock:
            return len(self._sessions)


# ─── Application singleton ────────────────────────────────────────────────────

# One store shared across all requests in the process lifetime.
session_store = SessionStore()
