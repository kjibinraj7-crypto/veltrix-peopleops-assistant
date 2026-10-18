"""
tests/test_access_control.py
──────────────────────────────
Unit tests for role-based access control.

Covers (task requirement):
- Employee sees only access_level='all' documents
- Manager sees 'all' + 'managers' documents
- HR sees all document access levels
- Leave data visibility per role
- Bulk data request refusal
"""

from __future__ import annotations

import pytest
from src.security.access_control import (
    can_role_read_doc,
    can_access_leave_data,
    is_bulk_data_request,
)


class TestDocumentAccess:
    """Tests for can_role_read_doc()"""

    def test_employee_can_read_all_docs(self):
        assert can_role_read_doc("employee", "all") is True

    def test_employee_cannot_read_manager_docs(self):
        assert can_role_read_doc("employee", "managers") is False

    def test_employee_cannot_read_hr_docs(self):
        assert can_role_read_doc("employee", "hr") is False

    def test_manager_can_read_all_docs(self):
        assert can_role_read_doc("manager", "all") is True

    def test_manager_can_read_manager_docs(self):
        assert can_role_read_doc("manager", "managers") is True

    def test_manager_cannot_read_hr_docs(self):
        assert can_role_read_doc("manager", "hr") is False

    def test_hr_can_read_all_access_levels(self):
        assert can_role_read_doc("hr", "all") is True
        assert can_role_read_doc("hr", "managers") is True
        assert can_role_read_doc("hr", "hr") is True

    def test_unknown_role_cannot_read_anything(self):
        assert can_role_read_doc("unknown", "all") is False


@pytest.mark.usefixtures("override_db")
class TestLeaveDataAccess:
    """Tests for can_access_leave_data()"""

    def _make_employee(self, employee_id, role, manager_id=None):
        from src.data_access.employee_repo import Employee
        return Employee(
            employee_id=employee_id,
            full_name="Test User",
            email="test@veltrix.example",
            phone="",
            region="IN",
            join_date="2020-01-01",
            level="L3",
            role=role,
            manager_id=manager_id,
        )

    def test_employee_can_see_own_data(self):
        requester = self._make_employee("E2004", "employee", "E2002")
        assert can_access_leave_data(requester, "E2004") is True

    def test_employee_cannot_see_others_data(self):
        requester = self._make_employee("E2004", "employee", "E2002")
        assert can_access_leave_data(requester, "E2005") is False

    def test_manager_can_see_own_data(self):
        requester = self._make_employee("E2002", "manager", "E2001")
        assert can_access_leave_data(requester, "E2002") is True

    def test_manager_can_see_direct_report(self):
        # E2002 manages E2004 (per conftest seed data)
        requester = self._make_employee("E2002", "manager", "E2001")
        assert can_access_leave_data(requester, "E2004") is True

    def test_manager_cannot_see_non_report(self):
        # E2002 does NOT manage E3004 (different region/manager)
        requester = self._make_employee("E2002", "manager", "E2001")
        assert can_access_leave_data(requester, "E3004") is False

    def test_hr_can_see_any_employee(self):
        requester = self._make_employee("E1001", "hr")
        assert can_access_leave_data(requester, "E2004") is True
        assert can_access_leave_data(requester, "E3004") is True
        assert can_access_leave_data(requester, "E4004") is True


class TestBulkDataGuard:
    """Tests for is_bulk_data_request()"""

    def test_detects_all_employees(self):
        assert is_bulk_data_request("Show me all employees' leave balances") is True

    def test_detects_everyone(self):
        assert is_bulk_data_request("What is everyone's annual leave?") is True

    def test_detects_list_all(self):
        assert is_bulk_data_request("list all requests") is True

    def test_detects_list_everyone(self):
        assert is_bulk_data_request("list everyone balances") is True

    def test_detects_show_all_employees(self):
        assert is_bulk_data_request("show all employees leave balances") is True

    def test_detects_every_employee(self):
        assert is_bulk_data_request("Give me every employee's remaining leave") is True

    def test_does_not_flag_single_employee(self):
        assert is_bulk_data_request("What is my leave balance?") is False

    def test_does_not_flag_own_records(self):
        assert is_bulk_data_request("Show all my leave requests") is False

    def test_does_not_flag_policy_questions(self):
        assert is_bulk_data_request("How many sick days do I get?") is False
