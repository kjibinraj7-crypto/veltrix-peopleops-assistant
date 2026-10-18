"""src/tools/get_team_leave.py"""
from __future__ import annotations
from src.data_access.employee_repo import Employee
from src.data_access.leave_repo import get_team_requests_in_range
from src.utils.logging import get_logger

logger = get_logger(__name__)


def run(args: dict, requester: Employee) -> dict:
    """
    Get team leave in a date range. Manager only.
    Args: start_date (YYYY-MM-DD), end_date (YYYY-MM-DD)
    """
    if requester.role != "manager":
        raise PermissionError("get_team_leave is only available to managers")

    start_date = args.get("start_date", "")
    end_date = args.get("end_date", "")
    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required")

    requests = get_team_requests_in_range(requester.employee_id, start_date, end_date)
    logger.info("Team leave retrieved",
                extra={"employee_id": requester.employee_id, "count": len(requests)})
    return {
        "status": "success",
        "data": [
            {
                "employee_id":  r.employee_id,
                "leave_type":   r.leave_type,
                "start_date":   r.start_date,
                "end_date":     r.end_date,
                "working_days": r.working_days,
                "status":       r.status,
            }
            for r in requests
        ],
        "message": f"Found {len(requests)} team leave request(s) between {start_date} and {end_date}",
    }
