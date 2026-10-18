"""src/tools/cancel_leave.py — Cancel a leave request with full validation."""
from __future__ import annotations
from datetime import date
from src.config import get_settings
from src.data_access.employee_repo import Employee
from src.data_access.leave_repo import get_request_by_id, cancel_leave_request
from src.utils.logging import get_logger

logger = get_logger(__name__)


def validate(args: dict, requester: Employee) -> dict:
    """Validate a cancel_leave request. Returns validated args or raises ValueError."""
    request_id = args.get("request_id", "")
    today = get_settings().get_today()

    if not request_id:
        raise ValueError("request_id is required.")

    req = get_request_by_id(request_id)
    if req is None:
        raise ValueError("Leave request not found.")

    # 1. Must own the request
    if req.employee_id != requester.employee_id:
        raise PermissionError("You can only cancel your own leave requests.")

    # 2. Must be pending or approved
    if req.status not in ("pending", "approved"):
        raise ValueError(f"Cannot cancel a request with status '{req.status}'.")

    # 3. start_date must be AFTER today (strictly)
    start = date.fromisoformat(req.start_date)
    if start <= today:
        raise ValueError(
            f"Cannot cancel leave starting on {req.start_date} "
            f"(start date must be after today, {today.isoformat()})."
        )

    return {
        "request_id":  request_id,
        "leave_type":  req.leave_type,
        "start_date":  req.start_date,
        "end_date":    req.end_date,
        "working_days": req.working_days,
        "employee_id": requester.employee_id,
    }


def execute(validated_args: dict, requester: Employee) -> dict:
    """Execute a pre-validated cancel_leave (called after confirmation)."""
    cancel_leave_request(validated_args["request_id"], requester.employee_id)
    return {
        "status": "success",
        "data": validated_args,
        "message": (
            f"✓ Leave request cancelled successfully "
            f"({validated_args['start_date']} to {validated_args['end_date']}). "
            f"{validated_args['working_days']} day(s) restored to your {validated_args['leave_type']} balance."
        ),
    }


def run(args: dict, requester: Employee) -> dict:
    """Entry point from tool registry. Validates AND executes (called after confirmation)."""
    validated = validate(args, requester)
    return execute(validated, requester)
