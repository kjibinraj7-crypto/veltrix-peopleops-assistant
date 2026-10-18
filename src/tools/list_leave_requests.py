"""src/tools/list_leave_requests.py"""
from __future__ import annotations
from src.data_access.employee_repo import Employee, get_employee_by_id
from src.data_access.leave_repo import get_leave_requests
from src.security.access_control import LEAVE_DATA_REFUSAL, can_access_leave_data
from src.utils.logging import get_logger

logger = get_logger(__name__)


def run(args: dict, requester: Employee) -> dict:
    """
    List leave requests for an employee.
    Args: employee_id (str), status (str, optional: 'pending'|'approved'|'cancelled')
    """
    target_id = args.get("employee_id", requester.employee_id)
    status = args.get("status")  # None = all statuses

    if get_employee_by_id(target_id) is None:
        raise PermissionError(LEAVE_DATA_REFUSAL)

    if not can_access_leave_data(requester, target_id):
        raise PermissionError(LEAVE_DATA_REFUSAL)

    requests = get_leave_requests(target_id, status)
    logger.info("Leave requests listed",
                extra={"employee_id": requester.employee_id, "target": target_id, "count": len(requests)})
    return {
        "status": "success",
        "data": [
            {
                "request_id":  r.request_id,
                "leave_type":  r.leave_type,
                "start_date":  r.start_date,
                "end_date":    r.end_date,
                "working_days": r.working_days,
                "status":      r.status,
                "created_at":  r.created_at,
            }
            for r in requests
        ],
        "message": f"Found {len(requests)} leave request(s) for {target_id}",
    }
