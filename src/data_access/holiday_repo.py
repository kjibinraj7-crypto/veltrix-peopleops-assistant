"""
src/data_access/holiday_repo.py
────────────────────────────────
Database access for the `holidays` table.

The `holidays` table is created and populated during ingestion (Phase 2).
At this stage the table may not yet exist — all functions handle that gracefully.

Schema (created by holiday_extractor.py during ingestion):
    CREATE TABLE IF NOT EXISTS holidays (
        region   TEXT NOT NULL,
        date     TEXT NOT NULL,   -- YYYY-MM-DD
        name     TEXT NOT NULL,
        year     INTEGER NOT NULL,
        doc_id   TEXT NOT NULL,
        PRIMARY KEY (region, date)
    );
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.data_access.db import get_db
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Holiday:
    region: str      # 'IN', 'US', 'DE'
    date: str        # YYYY-MM-DD
    name: str
    year: int
    doc_id: str


def get_holidays_for_region(region: str, year: Optional[int] = None) -> list[Holiday]:
    """
    Return all holidays for a given region, optionally filtered by year.
    Returns an empty list if the holidays table does not yet exist (pre-ingestion).
    """
    conn = get_db()
    try:
        if year is not None:
            rows = conn.execute(
                """
                SELECT region, date, name, year, doc_id
                FROM   holidays
                WHERE  region = ? AND year = ?
                ORDER BY date
                """,
                (region, year),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT region, date, name, year, doc_id
                FROM   holidays
                WHERE  region = ?
                ORDER BY date
                """,
                (region,),
            ).fetchall()
        return [
            Holiday(
                region=r["region"],
                date=r["date"],
                name=r["name"],
                year=r["year"],
                doc_id=r["doc_id"],
            )
            for r in rows
        ]
    except Exception as exc:
        # Table may not exist before ingestion completes
        logger.warning(
            "Could not query holidays table (may not exist yet)",
            extra={"region": region, "error": str(exc)},
        )
        return []


def get_holiday_dates_for_region(region: str, year: Optional[int] = None) -> set[str]:
    """
    Return a set of holiday date strings ('YYYY-MM-DD') for a region.
    Used by working_days.py for fast membership testing.
    """
    return {h.date for h in get_holidays_for_region(region, year)}
