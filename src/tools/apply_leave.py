"""src/tools/apply_leave.py — Apply for leave with full validation."""
from __future__ import annotations
from datetime import date
from src.config import get_settings
from src.data_access.employee_repo import Employee
from src.data_access.leave_repo import (
    get_balance_days,
    get_active_requests_in_range,
    create_leave_request,
)
from src.utils.working_days import count_working_days
from src.utils.logging import get_logger

logger = get_logger(__name__)


def validate(args: dict, requester: Employee) -> dict:
    """
    Validate an apply_leave request. Returns proposal dict or raises ValueError.
    Does NOT write to DB (that happens on confirmation).
    """
    leave_type = (args.get("leave_type") or "").lower().strip()
    start_str = args.get("start_date", "")
    end_str = args.get("end_date", "")
    today = get_settings().get_today()

    # 1. leave_type must be 'annual' or 'sick'
    if leave_type not in ("annual", "sick"):
        raise ValueError(f"Invalid leave_type '{leave_type}'. Must be 'annual' or 'sick'.")

    # 2. Parse dates
    try:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
    except (ValueError, TypeError):
        raise ValueError("Dates must be in YYYY-MM-DD format.")

    # 3. Both dates must be in 2026
    if start.year != 2026 or end.year != 2026:
        raise ValueError("Leave dates must be in 2026.")

    # 4. start_date >= today
    if start < today:
        raise ValueError(f"start_date {start_str} is in the past.")

    # 5. end_date >= start_date
    if end < start:
        raise ValueError("end_date must be on or after start_date.")

    # 6. Calculate working days
    working_days = count_working_days(start_str, end_str, requester.region)
    if working_days == 0:
        raise ValueError("The selected date range contains no working days (all weekends or holidays).")

    # 7. Check balance
    balance = get_balance_days(requester.employee_id, leave_type)
    if working_days > balance:
        raise ValueError(
            f"Insufficient {leave_type} leave balance. "
            f"Requested: {working_days} day(s), Available: {balance} day(s)."
        )

    # 8. Check for overlapping requests
    overlaps = get_active_requests_in_range(requester.employee_id, start_str, end_str)
    if overlaps:
        overlap_dates = f"{overlaps[0].start_date} to {overlaps[0].end_date}"
        raise ValueError(f"Leave overlaps with an existing request ({overlap_dates}).")

    return {
        "employee_id": requester.employee_id,
        "leave_type":  leave_type,
        "start_date":  start_str,
        "end_date":    end_str,
        "working_days": working_days,
    }


def execute(validated_args: dict, requester: Employee) -> dict:
    """Execute a pre-validated apply_leave (called after confirmation)."""
    request_id = create_leave_request(
        employee_id=requester.employee_id,
        leave_type=validated_args["leave_type"],
        start_date=validated_args["start_date"],
        end_date=validated_args["end_date"],
        working_days=validated_args["working_days"],
    )
    return {
        "status": "success",
        "data": {"request_id": request_id, **validated_args},
        "message": (
            f"✓ {validated_args['leave_type'].title()} leave applied successfully "
            f"({validated_args['start_date']} to {validated_args['end_date']}, "
            f"{validated_args['working_days']} working day(s)). Request ID: {request_id}"
        ),
    }


def run(args: dict, requester: Employee) -> dict:
    """Entry point from tool registry. Validates AND executes (called after confirmation)."""
    validated = validate(args, requester)
    return execute(validated, requester)
