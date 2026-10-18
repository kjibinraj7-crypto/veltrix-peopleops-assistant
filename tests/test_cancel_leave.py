"""tests/test_cancel_leave.py — cancel_leave validation unit tests."""
from __future__ import annotations
import pytest
from tests.conftest import EMPLOYEE_ID, MANAGER_ID


@pytest.mark.usefixtures("override_db")
class TestCancelLeave:

    def _requester(self, employee_id=EMPLOYEE_ID):
        from src.data_access.employee_repo import get_employee_by_id
        return get_employee_by_id(employee_id)

    def test_valid_cancel_pending(self):
        from src.tools.cancel_leave import validate
        req = self._requester()
        # req-pending-001 is pending and starts 2026-11-09 (future)
        result = validate({"request_id": "req-pending-001"}, req)
        assert result["request_id"] == "req-pending-001"

    def test_unknown_request_id(self):
        from src.tools.cancel_leave import validate
        req = self._requester()
        with pytest.raises(ValueError, match="not found"):
            validate({"request_id": "nonexistent-id"}, req)

    def test_wrong_owner_rejected(self):
        from src.tools.cancel_leave import validate
        # Manager trying to cancel employee's request
        manager = self._requester(MANAGER_ID)
        with pytest.raises(PermissionError, match="own"):
            validate({"request_id": "req-pending-001"}, manager)

    def test_past_request_rejected(self):
        from src.tools.cancel_leave import validate
        req = self._requester()
        # req-001 is approved and start_date is 2026-02-16 (past)
        with pytest.raises(ValueError, match="start date must be after today"):
            validate({"request_id": "req-001"}, req)

    def test_cancel_restores_balance(self):
        from src.tools.cancel_leave import validate, execute
        from src.data_access.leave_repo import get_balance_days
        req = self._requester()
        initial_balance = get_balance_days(EMPLOYEE_ID, "annual")
        validated = validate({"request_id": "req-pending-001"}, req)
        result = execute(validated, req)
        assert result["status"] == "success"
        new_balance = get_balance_days(EMPLOYEE_ID, "annual")
        # Should restore working_days (4 days for req-pending-001)
        assert new_balance == initial_balance + validated["working_days"]

    def test_already_cancelled_rejected(self):
        from src.tools.cancel_leave import validate, execute
        req = self._requester()
        # Cancel it once
        validated = validate({"request_id": "req-pending-001"}, req)
        execute(validated, req)
        # Try to cancel again — should fail (status is now 'cancelled')
        with pytest.raises(ValueError, match="status"):
            validate({"request_id": "req-pending-001"}, req)
