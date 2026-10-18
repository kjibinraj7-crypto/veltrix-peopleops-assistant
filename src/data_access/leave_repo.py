"""
src/data_access/leave_repo.py
──────────────────────────────
All database access for `leave_balances` and `leave_requests` tables.

Transaction rules (task §6.2 & §6.3):
- apply_leave: INSERT into leave_requests + UPDATE leave_balances in ONE transaction.
- cancel_leave: UPDATE status='cancelled' + ADD working_days back to balance in ONE transaction.
- Use the `transaction()` context manager from db.py — never commit/rollback manually.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.data_access.db import get_db, transaction
from src.utils.logging import get_logger

logger = get_logger(__name__)


# ─── Data models ─────────────────────────────────────────────────────────────

@dataclass
class LeaveBalance:
    employee_id: str
    leave_type: str    # 'annual' | 'sick'
    balance_days: float


@dataclass
class LeaveRequest:
    request_id: str
    employee_id: str
    leave_type: str    # 'annual' | 'sick'
    start_date: str    # YYYY-MM-DD
    end_date: str      # YYYY-MM-DD
    working_days: int
    status: str        # 'pending' | 'approved' | 'cancelled'
    created_at: str    # ISO datetime


# ─── Leave balance queries ────────────────────────────────────────────────────

def get_leave_balance(
    employee_id: str,
    leave_type: Optional[str] = None,
) -> list[LeaveBalance]:
    """
    Return leave balance(s) for an employee.
    If leave_type is None, returns both 'annual' and 'sick' rows.
    Returns an empty list if the employee has no balance rows.
    """
    conn = get_db()
    if leave_type:
        rows = conn.execute(
            """
            SELECT employee_id, leave_type, balance_days
            FROM   leave_balances
            WHERE  employee_id = ? AND leave_type = ?
            """,
            (employee_id, leave_type),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT employee_id, leave_type, balance_days
            FROM   leave_balances
            WHERE  employee_id = ?
            ORDER BY leave_type
            """,
            (employee_id,),
        ).fetchall()

    return [
        LeaveBalance(
            employee_id=r["employee_id"],
            leave_type=r["leave_type"],
            balance_days=r["balance_days"],
        )
        for r in rows
    ]


def get_balance_days(employee_id: str, leave_type: str) -> float:
    """
    Return the current balance for a specific leave type.
    Returns 0.0 if no row exists.
    """
    rows = get_leave_balance(employee_id, leave_type)
    return rows[0].balance_days if rows else 0.0


# ─── Leave request queries ────────────────────────────────────────────────────

def get_leave_requests(
    employee_id: str,
    status: Optional[str] = None,
) -> list[LeaveRequest]:
    """
    Return leave requests for an employee, optionally filtered by status.
    Ordered by created_at descending (newest first).
    """
    conn = get_db()
    if status:
        rows = conn.execute(
            """
            SELECT request_id, employee_id, leave_type, start_date,
                   end_date, working_days, status, created_at
            FROM   leave_requests
            WHERE  employee_id = ? AND status = ?
            ORDER BY created_at DESC
            """,
            (employee_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT request_id, employee_id, leave_type, start_date,
                   end_date, working_days, status, created_at
            FROM   leave_requests
            WHERE  employee_id = ?
            ORDER BY created_at DESC
            """,
            (employee_id,),
        ).fetchall()

    return [_row_to_request(r) for r in rows]


def get_request_by_id(request_id: str) -> Optional[LeaveRequest]:
    """Fetch a single leave request by its UUID."""
    conn = get_db()
    row = conn.execute(
        """
        SELECT request_id, employee_id, leave_type, start_date,
               end_date, working_days, status, created_at
        FROM   leave_requests
        WHERE  request_id = ?
        """,
        (request_id,),
    ).fetchone()
    return _row_to_request(row) if row else None


def get_active_requests_in_range(
    employee_id: str,
    start_date: str,
    end_date: str,
) -> list[LeaveRequest]:
    """
    Return pending or approved leave requests that overlap with [start_date, end_date].
    Used by apply_leave to detect date conflicts.
    Overlap condition: request.start_date <= end_date AND request.end_date >= start_date
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT request_id, employee_id, leave_type, start_date,
               end_date, working_days, status, created_at
        FROM   leave_requests
        WHERE  employee_id = ?
          AND  status IN ('pending', 'approved')
          AND  start_date <= ?
          AND  end_date   >= ?
        """,
        (employee_id, end_date, start_date),
    ).fetchall()
    return [_row_to_request(r) for r in rows]


def get_team_requests_in_range(
    manager_id: str,
    start_date: str,
    end_date: str,
) -> list[LeaveRequest]:
    """
    Return pending + approved leave requests for direct reports of manager_id
    that overlap with [start_date, end_date]. Used by get_team_leave tool.
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT lr.request_id, lr.employee_id, lr.leave_type, lr.start_date,
               lr.end_date, lr.working_days, lr.status, lr.created_at
        FROM   leave_requests lr
        JOIN   employees e ON lr.employee_id = e.employee_id
        WHERE  e.manager_id = ?
          AND  lr.status IN ('pending', 'approved')
          AND  lr.start_date <= ?
          AND  lr.end_date   >= ?
        ORDER BY lr.start_date
        """,
        (manager_id, end_date, start_date),
    ).fetchall()
    return [_row_to_request(r) for r in rows]


# ─── Write operations (always atomic) ────────────────────────────────────────

def create_leave_request(
    employee_id: str,
    leave_type: str,
    start_date: str,
    end_date: str,
    working_days: int,
) -> str:
    """
    Insert a new leave request (status='pending') and subtract working_days
    from the employee's balance. Both operations are done in ONE transaction.

    Returns the new request_id (UUID4).
    Raises on any DB error — transaction is automatically rolled back.
    """
    request_id = str(uuid.uuid4())
    created_at = datetime.now(tz=timezone.utc).isoformat()

    with transaction() as conn:
        # Step 1: Insert the leave request
        conn.execute(
            """
            INSERT INTO leave_requests
                (request_id, employee_id, leave_type, start_date, end_date,
                 working_days, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (request_id, employee_id, leave_type, start_date, end_date,
             working_days, created_at),
        )
        # Step 2: Deduct balance (must succeed in the same transaction)
        conn.execute(
            """
            UPDATE leave_balances
            SET    balance_days = balance_days - ?
            WHERE  employee_id  = ? AND leave_type = ?
            """,
            (working_days, employee_id, leave_type),
        )

    logger.info(
        "Leave request created",
        extra={
            "employee_id": employee_id,
            "request_id":  request_id,
            "leave_type":  leave_type,
            "working_days": working_days,
        },
    )
    return request_id


def cancel_leave_request(request_id: str, employee_id: str) -> None:
    """
    Set request status to 'cancelled' and restore working_days to the balance.
    Both operations are done in ONE transaction.

    Raises on any DB error — transaction is automatically rolled back.
    """
    # Fetch the request first to get leave_type and working_days
    req = get_request_by_id(request_id)
    if req is None:
        raise ValueError(f"Leave request {request_id} not found")

    with transaction() as conn:
        # Step 1: Mark as cancelled
        conn.execute(
            "UPDATE leave_requests SET status = 'cancelled' WHERE request_id = ?",
            (request_id,),
        )
        # Step 2: Restore balance
        conn.execute(
            """
            UPDATE leave_balances
            SET    balance_days = balance_days + ?
            WHERE  employee_id  = ? AND leave_type = ?
            """,
            (req.working_days, employee_id, req.leave_type),
        )

    logger.info(
        "Leave request cancelled",
        extra={
            "employee_id":  employee_id,
            "request_id":   request_id,
            "days_restored": req.working_days,
        },
    )


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _row_to_request(row: object) -> LeaveRequest:
    return LeaveRequest(
        request_id=row["request_id"],
        employee_id=row["employee_id"],
        leave_type=row["leave_type"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        working_days=row["working_days"],
        status=row["status"],
        created_at=row["created_at"],
    )
