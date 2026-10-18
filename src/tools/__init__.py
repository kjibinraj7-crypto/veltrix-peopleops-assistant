"""
src/tools/get_leave_balance.py  —  Read leave balance(s) for an employee.
src/tools/list_leave_requests.py — List leave requests.
src/tools/get_team_leave.py      — Manager-only team leave view.
src/tools/apply_leave.py         — Apply for leave (validates + proposes).
src/tools/cancel_leave.py        — Cancel a leave request (validates + proposes).
src/tools/registry.py            — Tool dispatch.

Auth rules:
  - All permission checks use employee_id from the HTTP header (requester object),
    NEVER from LLM arguments.
  - apply_leave / cancel_leave are self-service only.
  - get_team_leave is manager-only.
  - get_leave_balance / list_leave_requests: self, manager (direct reports), or HR.
"""
