"""
src/security/identity.py
─────────────────────────
Employee identity resolution from the X-Employee-Id HTTP header.

Rules (task §7.1):
- The database record is the ONLY source of the requester's identity, role, and region.
- Claims made inside messages ("I'm in HR") are always ignored.
- If X-Employee-Id is missing → HTTP 401.
- If X-Employee-Id is not found in the DB → HTTP 401 (SAME message as missing —
  do not reveal whether an employee ID exists).
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from src.data_access.employee_repo import Employee, get_employee_by_id
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Fixed 401 message — identical for missing header and unknown employee_id.
# This prevents information leakage about which employee IDs exist.
_UNAUTHORIZED_MSG = "Missing or invalid X-Employee-Id header."


async def resolve_identity(
    x_employee_id: str = Header(
        default=None,
        alias="X-Employee-Id",
        description="Employee ID of the requesting user. Required on every request.",
    ),
) -> Employee:
    """
    FastAPI dependency: resolve the requester's identity from the X-Employee-Id header.

    Raises HTTP 401 if:
    - The header is absent or empty.
    - The employee_id is not found in the database.

    Usage in endpoint:
        @app.post("/chat")
        async def chat(requester: Employee = Depends(resolve_identity)):
            ...
    """
    if not x_employee_id or not x_employee_id.strip():
        logger.warning("Request missing X-Employee-Id header")
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_MSG)

    employee_id = x_employee_id.strip()
    employee = get_employee_by_id(employee_id)

    if employee is None:
        # Log the attempt using the (unknown) employee_id — still safe, no PII
        logger.warning(
            "X-Employee-Id not found in database",
            extra={"employee_id": employee_id},
        )
        # Same message as missing header — do not leak existence
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_MSG)

    logger.debug(
        "Identity resolved",
        extra={"employee_id": employee.employee_id, "role": employee.role, "region": employee.region},
    )
    return employee
