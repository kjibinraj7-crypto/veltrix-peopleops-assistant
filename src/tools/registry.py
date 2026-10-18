"""
src/tools/registry.py
──────────────────────
Tool registration and dispatch.
All auth checks live inside the individual tool modules.
"""
from __future__ import annotations

from src.data_access.employee_repo import Employee

TOOL_NAMES = frozenset({
    "get_leave_balance",
    "list_leave_requests",
    "get_team_leave",
    "apply_leave",
    "cancel_leave",
})


def execute_tool(tool_name: str, args: dict, requester: Employee) -> dict:
    """
    Dispatch a tool call. Auth checks are inside each tool module.
    Raises PermissionError if unauthorized, ValueError if args are invalid.
    """
    if tool_name == "get_leave_balance":
        from src.tools.get_leave_balance import run
        return run(args, requester)

    elif tool_name == "list_leave_requests":
        from src.tools.list_leave_requests import run
        return run(args, requester)

    elif tool_name == "get_team_leave":
        from src.tools.get_team_leave import run
        return run(args, requester)

    elif tool_name == "apply_leave":
        from src.tools.apply_leave import run
        return run(args, requester)

    elif tool_name == "cancel_leave":
        from src.tools.cancel_leave import run
        return run(args, requester)

    else:
        raise ValueError(f"Unknown tool: {tool_name}")
