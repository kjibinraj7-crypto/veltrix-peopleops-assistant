"""
tests/test_working_days.py
───────────────────────────
Unit tests for working day calculation.

Covers (task requirement):
- Mon–Fri counting
- Weekend exclusion
- Regional holiday exclusion
- Ranges spanning weekends + holidays
"""

from __future__ import annotations

import pytest
from tests.conftest import override_db


@pytest.mark.usefixtures("override_db")
class TestWorkingDays:
    """Tests for src/utils/working_days.py"""

    def test_simple_weekday_range(self):
        """Mon–Fri within a single week = 5 days."""
        from src.utils.working_days import count_working_days
        # 2026-10-19 is Monday, 2026-10-23 is Friday
        assert count_working_days("2026-10-19", "2026-10-23", "IN") == 5

    def test_weekend_excluded(self):
        """Saturday and Sunday are not working days."""
        from src.utils.working_days import count_working_days
        # 2026-10-24 is Saturday, 2026-10-25 is Sunday
        assert count_working_days("2026-10-24", "2026-10-25", "IN") == 0

    def test_single_working_day(self):
        """A single Monday = 1 working day."""
        from src.utils.working_days import count_working_days
        assert count_working_days("2026-10-19", "2026-10-19", "IN") == 1

    def test_range_spanning_weekend(self):
        """A full week Mon–Sun = 5 working days."""
        from src.utils.working_days import count_working_days
        assert count_working_days("2026-10-19", "2026-10-25", "IN") == 5

    def test_holiday_excluded_in(self):
        """Republic Day (2026-01-26) is excluded for IN region."""
        from src.utils.working_days import count_working_days
        # 2026-01-26 is a Monday — normally 1 working day, but it's a holiday
        result = count_working_days("2026-01-26", "2026-01-26", "IN")
        assert result == 0

    def test_holiday_not_excluded_for_other_region(self):
        """IN holiday should NOT be excluded for US employees."""
        from src.utils.working_days import count_working_days
        # 2026-01-26 is a Monday — for US it's a regular working day
        result = count_working_days("2026-01-26", "2026-01-26", "US")
        assert result == 1

    def test_zero_days_when_end_before_start(self):
        """Invalid range (end < start) returns 0."""
        from src.utils.working_days import count_working_days
        assert count_working_days("2026-10-23", "2026-10-19", "IN") == 0

    def test_multi_week_range(self):
        """Two full working weeks = 10 days."""
        from src.utils.working_days import count_working_days
        # 2026-10-19 Mon to 2026-10-30 Fri = 2 weeks = 10 days
        assert count_working_days("2026-10-19", "2026-10-30", "IN") == 10
