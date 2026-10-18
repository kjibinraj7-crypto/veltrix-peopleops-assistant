"""
src/security/pii_filter.py
───────────────────────────
PII masking and suppression for logs, responses, LLM prompts, and tool_calls args.

Personal data definition (task §7.4):
    full_name, email, phone

Rules by context:
  Context              | Rule
  ─────────────────────|──────────────────────────────────────────────────────
  Logs                 | Never log full_name, email, phone. Use employee_id only.
  Responses & tool args| Emails masked as a***@example.com
                       | Phones: NEVER shown (not even masked)
                       | Names: only of employees requester can see
  LLM prompts          | Send only PII needed for the current request
"""

from __future__ import annotations

import re
from typing import Any

from src.data_access.employee_repo import Employee

# ─── Email masking ────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)"
)


def mask_email(email: str) -> str:
    """
    Mask an email address: first character + *** + @domain.
    Example: "someone@example.com" → "s***@example.com"
    """
    if not email or "@" not in email:
        return "***@***"
    local, domain = email.split("@", 1)
    return f"{local[0]}***@{domain}"


def mask_emails_in_text(text: str) -> str:
    """Replace all email addresses in a text string with masked versions."""
    def _replace(m: re.Match) -> str:
        return f"{m.group(1)[0]}***@{m.group(2)}"
    return _EMAIL_RE.sub(_replace, text)


# ─── Phone suppression ────────────────────────────────────────────────────────

# A candidate phone number: an optional leading + or (, then 9+ digit-ish
# characters, ending in a digit. Anything that survives tokenisation with 10-15
# digits and does not look like a date is treated as a phone number.
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\w])[+(]?\d[\d\s\-().]{7,}\d(?![\w])")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def strip_phones_in_text(text: str) -> str:
    """
    Remove phone-number-looking substrings from free text.

    Phones are NEVER shown, not even masked (task §7.4). Dates and other
    short numeric strings (leave balances, money amounts, effective dates) are
    deliberately left untouched.
    """
    def _replace(match: re.Match) -> str:
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        if 10 <= len(digits) <= 15 and not _DATE_RE.search(candidate):
            return "[phone number removed]"
        return candidate

    return _PHONE_CANDIDATE_RE.sub(_replace, text)


# ─── Employee data sanitization ───────────────────────────────────────────────

def sanitize_employee_for_response(employee: Employee) -> dict[str, Any]:
    """
    Convert an Employee to a dict safe for API responses.
    - full_name: included (requester must have already been authorized to see it)
    - email: masked
    - phone: suppressed entirely
    - All other fields: included as-is (not PII per task definition)
    """
    return {
        "employee_id": employee.employee_id,
        "full_name":   employee.full_name,
        "email":       mask_email(employee.email),
        # phone is intentionally omitted
        "region":      employee.region,
        "join_date":   employee.join_date,
        "level":       employee.level,
        "role":        employee.role,
        "manager_id":  employee.manager_id,
    }


def sanitize_tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """
    Sanitize tool_call args before including them in the API response.
    - Mask any email-looking values.
    - Remove any phone-looking values.
    """
    safe_args: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            if "@" in value:
                value = mask_email(value)
            elif _looks_like_phone(value):
                continue  # suppress entirely
        safe_args[key] = value
    return safe_args


def _looks_like_phone(value: str) -> bool:
    """Heuristic: does this string look like a phone number?"""
    # Strip common formatting
    stripped = re.sub(r"[\s\-+().x]", "", value)
    return stripped.isdigit() and 7 <= len(stripped) <= 15


# ─── Prompt PII minimization ──────────────────────────────────────────────────

def build_requester_context(requester: Employee) -> dict[str, str]:
    """
    Build the minimal PII context to include in the LLM system prompt.
    Only include what is strictly needed for the current request.

    The LLM needs:
    - employee_id: for tool calls
    - full_name: for personalized greeting
    - role + region: for access control decisions in the prompt
    - manager_id: for manager-specific logic

    The LLM does NOT need: email, phone, join_date, level.
    """
    return {
        "employee_id": requester.employee_id,
        "full_name":   requester.full_name,   # Used for greeting only
        "role":        requester.role,
        "region":      requester.region,
        "manager_id":  requester.manager_id or "",
    }
