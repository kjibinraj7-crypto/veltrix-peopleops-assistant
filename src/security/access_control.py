"""
src/security/access_control.py
────────────────────────────────
Role-based access control (RBAC) for documents and leave data.

Access control matrix (task §3.1 and §7.2 / §7.3):

  Role       | Documents             | Leave data          | Apply/Cancel
  -----------|-----------------------|---------------------|-------------
  employee   | access_level='all'    | Own only            | Own only
  manager    | 'all' + 'managers'    | Own + direct reports| Own only
  hr         | all access levels     | Any one employee    | Own only

Critical rules:
- Document access is enforced BEFORE chunks are sent to the LLM (pre-filter).
  Removing chunks AFTER the LLM sees them does NOT satisfy must-pass check #2.
- Never reveal that a restricted document exists (task §7.2).
- HR can view any ONE employee at a time — bulk requests are always refused.
"""

from __future__ import annotations

import re

from src.data_access.employee_repo import Employee, get_direct_report_ids
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ─── Document access ──────────────────────────────────────────────────────────

# Which access_level values each role can read
_ROLE_DOC_PERMISSIONS: dict[str, frozenset[str]] = {
    "employee": frozenset({"all"}),
    "manager":  frozenset({"all", "managers"}),
    "hr":       frozenset({"all", "managers", "hr"}),
}


def can_role_read_doc(role: str, access_level: str) -> bool:
    """
    Return True if the given role is permitted to read a document
    with the given access_level.

    Used in the pre-LLM retrieval filter (access_filter.py).
    """
    allowed = _ROLE_DOC_PERMISSIONS.get(role, frozenset())
    return access_level in allowed


# ─── Leave data access ────────────────────────────────────────────────────────

# One fixed refusal for any leave-data request the requester may not see. It is
# deliberately identical whether or not the target employee exists, so the
# response cannot be used as an existence oracle (task §7.3).
LEAVE_DATA_REFUSAL = "I can't share information about that employee."


def can_access_leave_data(requester: Employee, target_employee_id: str) -> bool:
    """
    Return True if the requester is permitted to view leave data for target_employee_id.

    Rules:
    - employee: can only view their own data.
    - manager:  can view own data + direct reports' data.
    - hr:       can view any single employee's data.

    Note: HR bulk queries ("show all employees") are handled separately in the
    router and always refused — this function is for single-employee lookups.
    """
    # Everyone can see their own data
    if requester.employee_id == target_employee_id:
        return True

    if requester.role == "hr":
        return True  # HR can view any individual employee

    if requester.role == "manager":
        direct_report_ids = get_direct_report_ids(requester.employee_id)
        return target_employee_id in direct_report_ids

    # role == "employee" → can only see own data (already handled above)
    return False


def can_use_tool(requester: Employee, tool_name: str, target_employee_id: str | None = None) -> bool:
    """
    Gate for tool-level authorization.

    - get_leave_balance / list_leave_requests: requester, their manager, or HR.
    - get_team_leave: managers only.
    - apply_leave / cancel_leave: self only.
    """
    if tool_name in ("get_leave_balance", "list_leave_requests"):
        if target_employee_id is None:
            return False
        return can_access_leave_data(requester, target_employee_id)

    if tool_name == "get_team_leave":
        return requester.role == "manager"

    if tool_name in ("apply_leave", "cancel_leave"):
        # Self-service only — no acting on behalf of others
        return True  # The tool itself enforces that requester == target

    return False


# ─── Bulk data guard ──────────────────────────────────────────────────────────

# Patterns describing a *group* of people / all records. Word-boundary matched so
# that legitimate self-service queries ("show all my requests") are not refused.
_BULK_PATTERNS = [
    r"\ball\s+employees?\b",
    r"\bevery\s+employees?\b",
    r"\beach\s+employees?\b",
    r"\ball\s+staff\b",
    r"\ball\s+people\b",
    r"\b(everyone|everybody)\b",
    r"\ball\s+team(\s+members?)?\b",
    r"\bentire\s+team\b",
    r"\bwhole\s+team\b",
    r"\ball\s+balances?\b",
    r"\ball\s+leave\s+requests?\b",
    r"\ball\s+requests?\b",
    r"\ball\s+leave\b",
    r"\ball\s+records?\b",
]

_BULK_RE = re.compile("|".join(_BULK_PATTERNS), re.IGNORECASE)


def is_bulk_data_request(message: str) -> bool:
    """
    Heuristic check for bulk data requests that must be refused for ALL roles,
    including HR. Examples: "show all employees' balances", "list every request",
    "list everyone balances".

    The LLM is also instructed to refuse bulk requests, but this pre-LLM guard
    makes the refusal deterministic. Word-boundary patterns avoid false positives
    on legitimate single-employee queries (e.g. "all my leave requests").
    """
    if not message:
        return False
    return _BULK_RE.search(message) is not None
