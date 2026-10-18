"""
src/utils/logging.py
────────────────────
PII-safe structured JSON logger.

Rules (from task §7.4):
- Never log full_name, email, or phone. Use employee_id only.
- Every log entry is JSON-formatted for easy parsing / forwarding.
- Standard fields on every entry: timestamp, level, request_id, employee_id, message.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class PIISafeJSONFormatter(logging.Formatter):
    """
    Format log records as single-line JSON objects.
    Automatically scrubs known PII field names from the extra dict.
    """

    # Field names that must never appear in logs
    _BLOCKED_FIELDS = frozenset({"full_name", "name", "email", "phone", "password"})

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
        }

        # Carry over safe contextual fields from `extra=` kwarg
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in logging.LogRecord.__dict__:
                continue
            # Skip standard LogRecord attributes
            if key in {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "pathname", "process", "processName", "relativeCreated",
                "stack_info", "thread", "threadName", "exc_info", "exc_text",
            }:
                continue
            # Block PII fields
            if key in self._BLOCKED_FIELDS:
                continue
            entry[key] = value

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def _build_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(PIISafeJSONFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Return a PII-safe JSON logger.

    Usage:
        logger = get_logger(__name__)
        logger.info("leave applied",
                    extra={"employee_id": "E1234", "route": "tool", "corpus_tokens": 820})

    Safe extra fields: employee_id, request_id, route, corpus_tokens, latency_ms, status
    Blocked fields:    full_name, email, phone (will be silently dropped)
    """
    logger = _build_logger(name)
    logger.setLevel(level)
    return logger


# Module-level root application logger
app_logger = get_logger("veltrix")
