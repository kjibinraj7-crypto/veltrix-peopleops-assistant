"""
src/utils/date_utils.py
────────────────────────
Date parsing utilities and relative-date resolution.

Rules (task §8.4):
- Today = value of APP_TODAY env var, or system date if not set.
- A date without a year defaults to 2026.
- Weeks start on Monday.
- "Next week" = Mon–Fri of the calendar week AFTER today.
- "Next Friday" = the first Friday strictly after today.
- If a date is ambiguous or already passed → caller must ask for clarification.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional


def get_monday_of_next_week(today: date) -> date:
    """Return the Monday of the calendar week after today."""
    # days_until_next_monday: if today is Mon (weekday=0), skip 7 days
    days_ahead = 7 - today.weekday()  # weekday: Mon=0, Sun=6
    return today + timedelta(days=days_ahead)


def get_next_weekday(today: date, weekday: int) -> date:
    """
    Return the first occurrence of `weekday` strictly after today.
    weekday: Mon=0, Tue=1, ..., Fri=4, Sat=5, Sun=6
    """
    days_ahead = weekday - today.weekday()
    if days_ahead <= 0:   # Target day is today or already passed this week
        days_ahead += 7
    return today + timedelta(days=days_ahead)


def parse_date_string(date_str: str, default_year: int = 2026) -> Optional[date]:
    """
    Parse a date string in several common formats.
    If no year is present, use default_year (2026 per task rules).

    Supported formats:
        YYYY-MM-DD  (ISO 8601 — preferred)
        DD/MM/YYYY
        DD-MM-YYYY
        YYYY/MM/DD

    Returns None if parsing fails.
    """
    date_str = (date_str or "").strip()
    if not date_str:
        return None

    # ISO first (fast path), then the common day/month/year variants.
    _FORMATS = (
        "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y",
        "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
        "%d %b", "%d %B", "%b %d", "%B %d",   # no year → default_year
    )
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(date_str, fmt)
        except ValueError:
            continue
        # strptime fills the year with 1900 when the format has no year.
        if parsed.year == 1900:
            parsed = parsed.replace(year=default_year)
        return parsed.date()

    return None


def normalize_to_iso(date_str: str) -> Optional[str]:
    """
    Normalize any supported date string to 'YYYY-MM-DD'.
    Returns None if parsing fails.
    """
    d = parse_date_string(date_str)
    return d.isoformat() if d else None


def is_date_in_year(d: date, year: int = 2026) -> bool:
    """Return True if the date falls within the given year."""
    return d.year == year


def dates_overlap(
    start1: str, end1: str,
    start2: str, end2: str,
) -> bool:
    """
    Return True if date ranges [start1, end1] and [start2, end2] overlap.
    All dates in 'YYYY-MM-DD' format.
    """
    s1, e1 = date.fromisoformat(start1), date.fromisoformat(end1)
    s2, e2 = date.fromisoformat(start2), date.fromisoformat(end2)
    return s1 <= e2 and e1 >= s2
