"""src/tools/get_leave_balance.py"""
from __future__ import annotations
from src.data_access.leave_repo import get_leave_balance
from src.data_access.employee_repo import Employee, get_employee_by_id
from src.security.access_control import LEAVE_DATA_REFUSAL, can_access_leave_data
from src.utils.logging import get_logger

logger = get_logger(__name__)


def run(args: dict, requester: Employee) -> dict:
    """
    Get leave balance for an employee.
    Args: employee_id (str), leave_type (str, optional: 'annual'|'sick')
    """
    target_id = args.get("employee_id", requester.employee_id)
    leave_type = args.get("leave_type")  # None = both types

    # Existence is checked first so that an unknown employee ID produces exactly
    # the same fixed refusal as an unauthorised one (task §7.3).
    if get_employee_by_id(target_id) is None:
        raise PermissionError(LEAVE_DATA_REFUSAL)

    # Auth check (always from requester object, never from args)
    if not can_access_leave_data(requester, target_id):
        raise PermissionError(LEAVE_DATA_REFUSAL)

    balances = get_leave_balance(target_id, leave_type)
    if not balances:
        raise ValueError(f"No leave balance found for employee {target_id}")

    logger.info("Leave balance retrieved",
                extra={"employee_id": requester.employee_id, "target": target_id})
    return {
        "status": "success",
        "data": [{"leave_type": b.leave_type, "balance_days": b.balance_days} for b in balances],
        "message": f"Leave balance retrieved for {target_id}",
    }
