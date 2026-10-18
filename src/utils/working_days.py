"""
src/utils/working_days.py
──────────────────────────
Working day calculation (Mon–Fri, excluding regional public holidays).

Rules (task §6.2):
- Working days = days in [start_date, end_date] that are Monday–Friday
  AND are not public holidays for the requester's region.
- Holidays must come from the `holidays` table (populated during ingestion).
- start_date and end_date are both inclusive.
"""

from __future__ import annotations

from datetime import date, timedelta

from src.data_access.holiday_repo import get_holiday_dates_for_region
from src.utils.logging import get_logger

logger = get_logger(__name__)


def count_working_days(start_date: str, end_date: str, region: str) -> int:
    """
    Count the number of working days in [start_date, end_date] inclusive
    for the given region.

    A working day is a Monday–Friday that is NOT a public holiday
    in the employee's region.

    Args:
        start_date: ISO date string 'YYYY-MM-DD'
        end_date:   ISO date string 'YYYY-MM-DD'
        region:     Region code ('IN', 'US', 'DE')

    Returns:
        int: Number of working days (0 if all days fall on weekends/holidays)
    """
    s = date.fromisoformat(start_date)
    e = date.fromisoformat(end_date)

    if e < s:
        return 0

    # Fetch all holiday dates for this region covering the year(s) in range
    years = set(range(s.year, e.year + 1))
    holidays: set[str] = set()
    for year in years:
        holidays |= get_holiday_dates_for_region(region, year)

    count = 0
    current = s
    while current <= e:
        # weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
        if current.weekday() < 5 and current.isoformat() not in holidays:
            count += 1
        current += timedelta(days=1)

    logger.debug(
        "Working days calculated",
        extra={
            "start_date":    start_date,
            "end_date":      end_date,
            "region":        region,
            "working_days":  count,
            "holidays_found": len(holidays),
        },
    )
    return count


def get_working_dates(start_date: str, end_date: str, region: str) -> list[str]:
    """
    Return the list of individual working day date strings in [start_date, end_date].
    Useful for overlap detection (checking each date against existing leave).
    """
    s = date.fromisoformat(start_date)
    e = date.fromisoformat(end_date)

    years = set(range(s.year, e.year + 1))
    holidays: set[str] = set()
    for year in years:
        holidays |= get_holiday_dates_for_region(region, year)

    result = []
    current = s
    while current <= e:
        if current.weekday() < 5 and current.isoformat() not in holidays:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result
