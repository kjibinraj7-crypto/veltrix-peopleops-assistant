"""tests/test_apply_leave.py — apply_leave validation unit tests."""
from __future__ import annotations
import pytest
from tests.conftest import EMPLOYEE_ID, MANAGER_ID


@pytest.mark.usefixtures("override_db")
class TestApplyLeave:

    def _requester(self, employee_id=EMPLOYEE_ID):
        from src.data_access.employee_repo import get_employee_by_id
        return get_employee_by_id(employee_id)

    def test_valid_annual_leave(self):
        from src.tools.apply_leave import validate
        req = self._requester()
        args = {"leave_type": "annual", "start_date": "2026-11-16", "end_date": "2026-11-18"}
        result = validate(args, req)
        assert result["working_days"] == 3
        assert result["leave_type"] == "annual"

    def test_invalid_leave_type(self):
        from src.tools.apply_leave import validate
        req = self._requester()
        with pytest.raises(ValueError, match="Invalid leave_type"):
            validate({"leave_type": "maternity", "start_date": "2026-11-16", "end_date": "2026-11-16"}, req)

    def test_past_start_date_rejected(self, monkeypatch):
        from src.tools.apply_leave import validate
        from src.config import get_settings
        # Set APP_TODAY to 2026-10-20 so 2026-01-05 is in the past
        monkeypatch.setenv("APP_TODAY", "2026-10-20")
        get_settings.cache_clear()
        req = self._requester()
        with pytest.raises(ValueError, match="in the past"):
            validate({"leave_type": "annual", "start_date": "2026-01-05", "end_date": "2026-01-06"}, req)
        get_settings.cache_clear()

    def test_end_before_start_rejected(self):
        from src.tools.apply_leave import validate
        req = self._requester()
        with pytest.raises(ValueError, match="on or after"):
            validate({"leave_type": "annual", "start_date": "2026-11-20", "end_date": "2026-11-18"}, req)

    def test_zero_working_days_rejected(self):
        from src.tools.apply_leave import validate
        req = self._requester()
        # 2026-11-21 and 2026-11-22 are Saturday and Sunday
        with pytest.raises(ValueError, match="no working days"):
            validate({"leave_type": "annual", "start_date": "2026-11-21", "end_date": "2026-11-22"}, req)

    def test_insufficient_balance_rejected(self):
        from src.tools.apply_leave import validate
        req = self._requester()
        # E2004 has 10 annual days — request 15
        with pytest.raises(ValueError, match="Insufficient"):
            validate({"leave_type": "annual", "start_date": "2026-11-16", "end_date": "2026-12-04"}, req)

    def test_overlap_rejected(self):
        from src.tools.apply_leave import validate
        req = self._requester()
        # req-pending-001 covers 2026-11-09 to 2026-11-13 (pending)
        with pytest.raises(ValueError, match="overlap"):
            validate({"leave_type": "annual", "start_date": "2026-11-10", "end_date": "2026-11-11"}, req)

    def test_non_2026_date_rejected(self):
        from src.tools.apply_leave import validate
        req = self._requester()
        with pytest.raises(ValueError, match="2026"):
            validate({"leave_type": "annual", "start_date": "2027-01-02", "end_date": "2027-01-03"}, req)

    def test_execute_creates_request_and_deducts_balance(self):
        from src.tools.apply_leave import validate, execute
        from src.data_access.leave_repo import get_balance_days, get_leave_requests
        req = self._requester()
        args = {"leave_type": "sick", "start_date": "2026-12-01", "end_date": "2026-12-01"}
        validated = validate(args, req)
        result = execute(validated, req)
        assert result["status"] == "success"
        assert "request_id" in result["data"]
        # Balance should decrease by 1
        new_balance = get_balance_days(EMPLOYEE_ID, "sick")
        assert new_balance == 11.0   # was 12.0
