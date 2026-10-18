"""
src/data_access/db.py
─────────────────────
Thread-safe SQLite connection manager.

Design choices:
- Uses threading.local() to give each thread its own connection.
  SQLite connections must not be shared across threads.
- Enables WAL (Write-Ahead Logging) mode for better concurrent reads.
- Enables foreign key enforcement.
- Returns rows as sqlite3.Row (dict-like access by column name).
- Provides a context manager for atomic transactions.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Thread-local storage: each thread gets its own connection
_local = threading.local()


def _get_connection() -> sqlite3.Connection:
    """
    Return the SQLite connection for the current thread.
    Creates a new connection if one does not already exist.
    """
    settings = get_settings()
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(
            settings.db_path,
            check_same_thread=False,   # We handle thread safety via _local
            timeout=10,
        )
        conn.row_factory = sqlite3.Row  # Rows accessible as dicts
        # WAL mode: readers don't block writers, writers don't block readers
        conn.execute("PRAGMA journal_mode=WAL")
        # Enforce foreign key constraints
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        logger.debug("SQLite connection opened", extra={"db": settings.db_path})
    return _local.conn


def get_db() -> sqlite3.Connection:
    """Return the thread-local database connection."""
    return _get_connection()


@contextmanager
def transaction() -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager for atomic database transactions.

    Usage:
        with transaction() as conn:
            conn.execute("INSERT INTO leave_requests ...")
            conn.execute("UPDATE leave_balances ...")
        # Both committed together — or both rolled back on exception.
    """
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_connection() -> None:
    """Close the current thread's SQLite connection (called on shutdown)."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None
        logger.debug("SQLite connection closed")
