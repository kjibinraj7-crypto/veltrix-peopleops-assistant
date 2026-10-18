"""
src/data_access/employee_repo.py
─────────────────────────────────
All database access for the `employees` table.

Security rules (task §7.1 & §7.4):
- The DB record is the ONLY trusted source of role, region, and name.
- Never trust user-supplied claims about identity.
- Callers log employee_id only — never full_name, email, or phone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.data_access.db import get_db
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Employee:
    """Represents one row from the employees table."""
    employee_id: str
    full_name: str
    email: str
    phone: str
    region: str          # 'IN', 'US', 'DE'
    join_date: str       # YYYY-MM-DD
    level: str           # 'L1' … 'L7'
    role: str            # 'employee', 'manager', 'hr'
    manager_id: Optional[str]


def get_employee_by_id(employee_id: str) -> Optional[Employee]:
    """
    Fetch a single employee by their primary key.
    Returns None if the employee does not exist.

    Callers must treat a None return the same as "not authorized"
    to avoid leaking whether an employee_id exists (task §7.3).
    """
    conn = get_db()
    row = conn.execute(
        """
        SELECT employee_id, full_name, email, phone, region,
               join_date, level, role, manager_id
        FROM   employees
        WHERE  employee_id = ?
        """,
        (employee_id,),
    ).fetchone()

    if row is None:
        return None

    return Employee(
        employee_id=row["employee_id"],
        full_name=row["full_name"],
        email=row["email"],
        phone=row["phone"],
        region=row["region"],
        join_date=row["join_date"],
        level=row["level"],
        role=row["role"],
        manager_id=row["manager_id"],
    )


def get_direct_reports(manager_id: str) -> list[Employee]:
    """
    Return the list of employees whose manager_id matches the given ID.
    Only direct reports — NOT transitive (not reports of reports).
    Used by get_team_leave and access control checks for managers.
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT employee_id, full_name, email, phone, region,
               join_date, level, role, manager_id
        FROM   employees
        WHERE  manager_id = ?
        ORDER BY employee_id
        """,
        (manager_id,),
    ).fetchall()

    return [
        Employee(
            employee_id=r["employee_id"],
            full_name=r["full_name"],
            email=r["email"],
            phone=r["phone"],
            region=r["region"],
            join_date=r["join_date"],
            level=r["level"],
            role=r["role"],
            manager_id=r["manager_id"],
        )
        for r in rows
    ]


def get_direct_report_ids(manager_id: str) -> set[str]:
    """
    Return a set of employee_ids who are direct reports of manager_id.
    Lightweight version of get_direct_reports for access-control checks.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT employee_id FROM employees WHERE manager_id = ?",
        (manager_id,),
    ).fetchall()
    return {r["employee_id"] for r in rows}
