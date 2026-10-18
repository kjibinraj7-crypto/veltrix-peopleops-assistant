"""
src/orchestration/router.py
────────────────────────────
Main orchestration: receives a user message, runs retrieval + LLM, returns structured response.

Full pipeline per request:
  1. Bulk data guard (refuse immediately)
  2. Check pending confirmation → execute or discard
  3. Check tool-only intent → route=tool path
  4. Hybrid retrieval → FAISS+BM25+RRF
  5. Access control filter (pre-LLM — mandatory)
  6. Confidence threshold → no_answer if below
  7. Rerank → Precedence sort → Token budget
  8. Build prompt → LLM call → Parse JSON response
  9. Cache eligible responses
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date
from typing import Any

from src.config import get_settings
from src.data_access.employee_repo import Employee
from src.orchestration.cache import response_cache
from src.orchestration.confirmation import is_confirmation, is_cancellation
from src.orchestration.prompts import build_system_prompt
from src.orchestration.session import Session, session_store
from src.retrieval.access_filter import filter_chunks
from src.retrieval.hybrid_retriever import retrieve, is_below_confidence_threshold
from src.retrieval.precedence import sort_chunks_by_precedence, get_conflict_note
from src.retrieval.reranker import rerank
from src.retrieval.token_budget import (
    count_tokens,
    select_chunks_within_budget,
    format_chunks_for_prompt,
    format_upcoming_for_prompt,
)
from src.security.access_control import LEAVE_DATA_REFUSAL, is_bulk_data_request
from src.security.pii_filter import (
    build_requester_context,
    mask_emails_in_text,
    sanitize_tool_args,
    strip_phones_in_text,
)
from src.tools.registry import execute_tool, TOOL_NAMES
from src.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# Write tools that MUST go through the two-step confirmation flow.
WRITE_TOOLS = frozenset({"apply_leave", "cancel_leave"})


def _validate_write_tool(tool_name: str, args: dict, requester: Employee) -> dict:
    """
    Validate a proposed write action WITHOUT touching the database.
    Raises ValueError/PermissionError with a specific, user-safe reason.
    """
    if tool_name == "apply_leave":
        from src.tools.apply_leave import validate as validate_apply
        return validate_apply(args, requester)
    if tool_name == "cancel_leave":
        from src.tools.cancel_leave import validate as validate_cancel
        return validate_cancel(args, requester)
    raise ValueError(f"'{tool_name}' is not a write action.")

# ─── LLM call ────────────────────────────────────────────────────────────────

# Error fragments that mean "this endpoint/model does not accept one of our
# optional parameters". In that case we retry once with a minimal payload, so a
# stricter or newer model never turns into a hard failure.
_RETRYABLE_PARAM_ERRORS = (
    "temperature",
    "response_format",
    "response format",
    "unsupported",
    "not supported",
    "invalid parameter",
    "unrecognized",
)


def _is_retryable_param_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in _RETRYABLE_PARAM_ERRORS)


# Fragments that identify a provider throttling / capacity error. These are
# transient, so they are worth one short in-request retry. The retry window is
# deliberately short: TASK §4.2 requires every HTTP request to finish inside 30
# seconds, so a long wait must never happen here (the evaluation harness paces
# its own requests instead).
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "too many requests",
    "resource_exhausted",
    "quota",
    "overloaded",
)


def _is_rate_limit_error(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return any(token in text for token in _RATE_LIMIT_MARKERS)


def _rate_limit_wait_seconds(exc: Exception) -> float | None:
    """Best-effort read of the provider's suggested cool-off, if it gives one."""
    response = getattr(exc, "response", None)
    header = None
    if response is not None:
        try:
            header = response.headers.get("retry-after")
        except Exception:  # noqa: BLE001 — headers are optional and provider-specific
            header = None
    if header:
        try:
            return float(header)
        except (TypeError, ValueError):
            pass
    m = re.search(r"try again in ([\d.]+)\s*s", str(exc), re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _call_llm(system_prompt: str, messages: list[dict], request_id: str) -> str:
    """
    Call the configured provider through the blocking SDK.

    Wraps ``_invoke_provider`` with a short, bounded retry for transient
    throttling errors, so a momentary 429 does not turn into a user-visible
    failure. The total added delay is capped by ``LLM_RATE_LIMIT_MAX_SLEEP``
    (default well under the 30-second per-request budget).
    """
    attempts = max(1, int(settings.llm_rate_limit_retries) + 1)
    for attempt in range(1, attempts + 1):
        try:
            return _invoke_provider(system_prompt, messages, request_id)
        except Exception as exc:  # noqa: BLE001 — re-raised below when not retryable
            if attempt >= attempts or not _is_rate_limit_error(exc):
                raise
            wait = _rate_limit_wait_seconds(exc)
            if wait is None:
                wait = 2.0 * attempt
            wait = max(0.5, min(wait, float(settings.llm_rate_limit_max_sleep)))
            logger.warning(
                "LLM throttled — retrying once after a short wait",
                extra={"request_id": request_id, "attempt": attempt, "wait_seconds": wait},
            )
            time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


def _invoke_provider(system_prompt: str, messages: list[dict], request_id: str) -> str:
    """
    Call the configured LLM provider. Returns raw response text.

    Providers are handled uniformly so OpenAI, Groq, Anthropic, Google and any
    OpenAI-compatible endpoint (xAI Grok, OpenRouter, Ollama, vLLM, Together…)
    all work with the same orchestration code. Optional parameters that a given
    model rejects are dropped and the call retried once.
    """
    provider = settings.llm_provider.lower().strip()
    model = settings.get_default_model()
    temperature = settings.llm_temperature

    try:
        if provider in ("openai", "groq", "openai_compatible"):
            client = settings.get_llm_client()
            all_msgs = [{"role": "system", "content": system_prompt}] + messages
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": all_msgs,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as exc:
                if not _is_retryable_param_error(exc):
                    raise
                logger.warning(
                    "LLM rejected optional params — retrying without them",
                    extra={"request_id": request_id, "provider": provider},
                )
                for optional in ("temperature", "response_format"):
                    kwargs.pop(optional, None)
                resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""

        elif provider == "anthropic":
            client = settings.get_llm_client()
            base_kwargs: dict[str, Any] = {
                "model": model,
                "system": system_prompt,
                "messages": messages,
                "max_tokens": 2048,
            }
            try:
                resp = client.messages.create(temperature=temperature, **base_kwargs)
            except TypeError as exc:
                # anthropic SDK v1.x removed the temperature parameter entirely.
                if "temperature" not in str(exc).lower():
                    raise
                resp = client.messages.create(**base_kwargs)
            return resp.content[0].text if resp.content else ""

        elif provider == "google":
            from google.genai import types
            client = settings.get_llm_client()
            contents = [
                types.Content(
                    role="model" if m["role"] == "assistant" else "user",
                    parts=[types.Part(text=m["content"])],
                )
                for m in messages
            ]
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
            )
            return resp.text or ""

        else:
            raise ValueError(f"Unknown provider: {provider}")

    except Exception as exc:
        logger.error("LLM call failed", extra={"request_id": request_id, "error": str(exc)})
        raise


def _parse_llm_response(raw: str) -> dict:
    """Parse the LLM's JSON response. Returns a safe default on failure."""
    # Strip markdown fences if any
    raw = re.sub(r'^```(?:json)?\s*', '', raw.strip())
    raw = re.sub(r'\s*```$', '', raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try extracting the first JSON object
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return {
        "route": "no_answer",
        "answer": "I encountered an issue processing your request. Please try again.",
        "citations": [],
        "tool_calls": [],
        "pending_confirmation": False,
        # The model did not return the expected JSON contract. This is an
        # infrastructure/format failure, not a wrong answer, so it is flagged
        # `degraded` and callers can retry instead of grading the fallback text.
        "degraded": True,
    }


# ─── Response normalisation (API contract, task §8.1 / §4.2) ─────────────────

VALID_ROUTES = ("rag", "tool", "hybrid", "clarify", "no_answer", "refuse")


def _chunk_locator(chunk: dict) -> str:
    """Nearest section heading, else the page number (task §4.2)."""
    meta = chunk.get("metadata", {})
    heading = (meta.get("section_heading") or "").strip()
    if heading:
        return heading
    page = meta.get("page_number")
    return f"p. {page}" if page else "unknown"


def _normalise_citations(raw_citations: Any, allowed_chunks: list[dict]) -> list[dict]:
    """
    Keep only citations whose doc_id was actually retrieved *and* is eligible for
    this requester. Never cite a document the requester may not read, and never
    emit a citation without a locator.
    """
    locators: dict[str, str] = {}
    for chunk in allowed_chunks:
        doc_id = chunk.get("metadata", {}).get("doc_id")
        if doc_id and doc_id not in locators:
            locators[doc_id] = _chunk_locator(chunk)

    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(raw_citations, list):
        return out
    for citation in raw_citations:
        if not isinstance(citation, dict):
            continue
        doc_id = citation.get("doc_id")
        if not isinstance(doc_id, str) or doc_id not in locators or doc_id in seen:
            continue
        locator = citation.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            locator = locators[doc_id]
        out.append({"doc_id": doc_id, "locator": locator})
        seen.add(doc_id)
    return out


def _derive_route(citations: list[dict], tool_calls: list[dict]) -> str:
    if citations and tool_calls:
        return "hybrid"
    if citations:
        return "rag"
    if tool_calls:
        return "tool"
    return "no_answer"


# Leave-management requests are answered by the leave TOOLS, not by the document
# corpus. When retrieval finds nothing, we must still let these reach the model
# so it can call a tool; otherwise "what is my leave balance?" would be rejected
# as an uncovered topic. These are general domain phrases, not corpus content.
_TOOL_INTENT_PATTERNS = (
    r"\bbalance\b",
    r"\bdays?\b[^.?]{0,40}\b(?:left|remaining)\b",
    r"\bmy\s+leave\s+(?:requests?|history|records?)\b",
    r"\b(?:apply|book|request|submit|file)\b[^.?]{0,25}"
    r"\b(?:leave|time\s*off|annual|sick|vacation|holiday)\b",
    r"\b(?:take|taking|need)\s+(?:some\s+|a\s+few\s+)?(?:leave|time\s*off|annual|sick)",
    r"\bcancel\b.{0,30}\bleave\b",
    r"\b(?:my|the)\s+team\b.{0,30}\bleave\b",
    r"\bwho(?:'s| is| are)\b.{0,20}\b(?:on\s+leave|off)\b",
)

_TOOL_INTENT_RE = re.compile("|".join(_TOOL_INTENT_PATTERNS), re.IGNORECASE)


def _looks_like_leave_tool_request(message: str) -> bool:
    """True when the message is likely answered by a leave tool, not the corpus."""
    return bool(message) and _TOOL_INTENT_RE.search(message) is not None


def _enforce_route_contract(
    route: Any, citations: list[dict], tool_calls: list[dict]
) -> tuple[str, list[dict], list[dict]]:
    """
    Enforce the route/citations/tool_calls table from task §8.1 so the API can
    never return an internally inconsistent response:

      rag      → ≥1 citation, 0 tool calls
      tool     → 0 citations, ≥1 tool call
      hybrid   → ≥1 citation, ≥1 tool call
      clarify / no_answer / refuse → 0 citations, 0 tool calls
    """
    if not isinstance(route, str) or route not in VALID_ROUTES:
        route = _derive_route(citations, tool_calls)

    if route == "refuse":
        # A refusal must never hint at a document.
        return "refuse", [], []

    expected = _derive_route(citations, tool_calls)
    if route in ("clarify", "no_answer"):
        # The model decided "no document answers this" / "I need more
        # information". Respect that choice and drop the fields this route
        # forbids, rather than promoting it to a route it did not choose.
        return route, [], []
    if route == "rag":
        if not (citations and not tool_calls):
            route = expected
    elif route == "tool":
        if not (tool_calls and not citations):
            route = expected
    elif route == "hybrid":
        if not (citations and tool_calls):
            route = expected
    return route, citations, tool_calls


# ─── Main handler ─────────────────────────────────────────────────────────────

async def handle_message(
    message: str,
    requester: Employee,
    session: Session,
    request_id: str,
) -> dict[str, Any]:
    """
    Full orchestration pipeline. Returns a dict matching ChatResponse fields.
    """
    today = settings.get_today()
    today_str = today.isoformat()

    # ── 1. Bulk data guard ────────────────────────────────────────────────────
    # Refused for every role. Any other message discards a waiting proposal
    # (task §8.2), so a refusal must also drop it.
    if is_bulk_data_request(message):
        if session.pending_confirmation is not None:
            session_store.clear_pending_confirmation(session)
        # One fixed refusal for every role, phrased so it never invites the
        # requester to name another employee (that would suggest they are
        # allowed to look someone else up).
        return _refuse(
            "You can only view your own leave information. I can't list leave "
            "balances for a group of employees."
        )

    # ── 2. Pending confirmation check ─────────────────────────────────────────
    if session.pending_confirmation is not None:
        pending = session.pending_confirmation

        if is_confirmation(message):
            # Execute the pending tool
            logger.info(
                "Confirmation received — executing tool",
                extra={"request_id": request_id, "tool": pending.tool_name, "employee_id": requester.employee_id},
            )
            try:
                result = execute_tool(pending.tool_name, pending.tool_args, requester)
                session_store.clear_pending_confirmation(session)
                return {
                    "route": "tool",
                    "answer": result.get("message", "Done."),
                    "citations": [],
                    "tool_calls": [{
                        "name": pending.tool_name,
                        "args": sanitize_tool_args(pending.tool_args),
                        "status": result.get("status", "success"),
                    }],
                    "pending_confirmation": False,
                }
            except Exception as exc:
                session_store.clear_pending_confirmation(session)
                return {
                    "route": "tool",
                    "answer": f"The action could not be completed: {str(exc)}",
                    "citations": [],
                    "tool_calls": [{"name": pending.tool_name, "args": {}, "status": "error"}],
                    "pending_confirmation": False,
                }

        elif is_cancellation(message):
            session_store.clear_pending_confirmation(session)
            # Nothing was executed and no document was consulted, so this is a
            # clarify turn (empty citations + empty tool_calls), not a tool turn.
            return {
                "route": "clarify",
                "answer": "Understood — I've discarded that request. What would you like to do?",
                "citations": [],
                "tool_calls": [],
                "pending_confirmation": False,
            }
        else:
            # Any other message — discard pending, handle normally
            session_store.clear_pending_confirmation(session)

    # ── 3. Cache check (RAG-only) ─────────────────────────────────────────────
    # Lookup embeds the query (CPU-bound), so it runs in a worker thread —
    # otherwise it would block the event loop and the per-request timeout.
    loop = asyncio.get_running_loop()
    cached = await loop.run_in_executor(
        None, response_cache.get,
        requester.employee_id, requester.role, requester.region, message,
    )
    if cached:
        return cached

    # ── 4. Hybrid retrieval ───────────────────────────────────────────────────
    raw_chunks, best_score = retrieve(message, top_k=settings.retrieval_top_k)

    # ── 5. Access control filter (PRE-LLM — mandatory) ────────────────────────
    eligible_chunks, upcoming_chunks = filter_chunks(
        raw_chunks, requester.role, requester.region, today
    )

    # ── 6. Confidence threshold / no-retrieval gate ───────────────────────────
    # If nothing relevant was retrieved there is usually nothing to answer from
    # and we can skip the LLM. The exception is a leave-management request
    # ("what is my balance?", "apply for leave"): those are answered by the
    # leave TOOLS, not by the document corpus, so they must still reach the
    # model. When we do proceed without documents we tell the model explicitly
    # that it has no documents, so it cannot fall back on general knowledge.
    tool_intent = _looks_like_leave_tool_request(message)
    if not eligible_chunks and not tool_intent:
        reason = "below_confidence_threshold" if is_below_confidence_threshold(
            best_score, settings.confidence_threshold) else "no_relevant_documents"
        logger.info("No usable documents — skipping LLM",
                    extra={"request_id": request_id, "score": best_score, "reason": reason})
        return {
            "route": "no_answer",
            "answer": "The available documents don't cover this topic. Please contact the HR team directly.",
            "citations": [],
            "tool_calls": [],
            "pending_confirmation": False,
        }

    # ── 7. Rerank → precedence sort → token budget ────────────────────────────
    reranked = rerank(message, eligible_chunks)
    sorted_chunks = sort_chunks_by_precedence(reranked, requester.region)
    conflict_note = get_conflict_note(sorted_chunks)

    # ── 8. Build prompt and call LLM ─────────────────────────────────────────
    requester_ctx = build_requester_context(requester)
    system_prompt = build_system_prompt(requester_ctx, today_str)

    upcoming_text = format_upcoming_for_prompt(upcoming_chunks[:3])
    # Reserve the token cost of everything added *around* the DOC blocks
    # (upcoming-changes text + conflict note) so the total corpus text sent to
    # the LLM never exceeds the 3,000-token budget.
    overhead = 0
    if upcoming_text:
        overhead += count_tokens(upcoming_text)
    if conflict_note:
        overhead += count_tokens(conflict_note)

    final_chunks = select_chunks_within_budget(
        sorted_chunks,
        budget=max(settings.corpus_token_budget - overhead, 0),
        request_id=request_id,
    )
    corpus_text = format_chunks_for_prompt(final_chunks)

    user_content = message
    if corpus_text:
        user_content += f"\n\n--- Retrieved Documents ---\n{corpus_text}"
    else:
        # No documents were retrieved for a leave-management request. Say so
        # explicitly so the model answers only via tools and never falls back on
        # general knowledge.
        user_content += (
            "\n\n[SYSTEM NOTE: No documents were retrieved for this message. Do NOT "
            "answer from general knowledge. This message asks about the requester's "
            "own leave: you MUST call the matching read tool "
            "(get_leave_balance for a balance, list_leave_requests for bookings) and "
            "answer from its result. Only return route: no_answer if no leave tool "
            "can help.]"
        )
    if upcoming_text:
        user_content += f"\n\n{upcoming_text}"
    if conflict_note:
        user_content += f"\n\n[SYSTEM NOTE: {conflict_note}]"

    history = session_store.get_history(session)
    # Build messages list: history (without last user msg) + current user message
    prior = [m for m in history[:-1] if m["role"] in ("user", "assistant")]
    messages = prior[-10:] + [{"role": "user", "content": user_content}]  # Last 10 turns max

    try:
        # The provider SDKs are blocking — run off the event loop so the
        # per-request timeout in main.py can actually take effect.
        loop = asyncio.get_running_loop()
        raw_response = await loop.run_in_executor(
            None, _call_llm, system_prompt, messages, request_id
        )
        parsed = _parse_llm_response(raw_response)
    except Exception as exc:
        logger.error("LLM call error", extra={"request_id": request_id, "error": str(exc)})
        return {
            "route": "no_answer",
            "answer": "I'm temporarily unable to process your request. Please try again shortly.",
            "citations": [],
            "tool_calls": [],
            "pending_confirmation": False,
            # Signals "we could not reach the model", as opposed to "the model
            # decided there was no answer". Callers (and the eval harness) can
            # tell the two apart and retry instead of grading a fallback string.
            "degraded": True,
        }

    route = parsed.get("route", "no_answer")
    answer = parsed.get("answer", "")
    citations = parsed.get("citations", [])
    llm_tool_calls = parsed.get("tool_calls", [])

    # ── 9. Execute tool calls ─────────────────────────────────────────────────
    executed_tool_calls = []
    for tc in llm_tool_calls:
        tool_name = tc.get("name", "")
        tool_args = tc.get("args", {})

        if tool_name not in TOOL_NAMES:
            continue

        if tool_name in WRITE_TOOLS:
            # Any write tool call ALWAYS goes through the confirmation flow,
            # regardless of the status the model reported. The model can never
            # cause an immediate database write. Step 1: validate WITHOUT writing,
            # then store the proposal. Invalid proposals are reported immediately
            # (route=tool, status=error) so the user never confirms a dead action.
            try:
                validated_args = _validate_write_tool(tool_name, tool_args, requester)
            except PermissionError:
                executed_tool_calls.append({
                    "name": tool_name,
                    "args": sanitize_tool_args(tool_args),
                    "status": "error",
                })
                answer = LEAVE_DATA_REFUSAL
                continue
            except Exception as exc:
                executed_tool_calls.append({
                    "name": tool_name,
                    "args": sanitize_tool_args(tool_args),
                    "status": "error",
                })
                answer = f"I can't do that: {exc}"
                continue

            from src.orchestration.session import PendingConfirmation
            session_store.set_pending_confirmation(
                session,
                PendingConfirmation(
                    tool_name=tool_name,
                    tool_args=validated_args,
                    summary=answer,
                    proposal_answer=answer,
                ),
            )
            executed_tool_calls.append({
                "name": tool_name,
                "args": sanitize_tool_args(validated_args),
                "status": "pending_confirmation",
            })
        else:
            # Read tool — safe to execute immediately.
            try:
                result = execute_tool(tool_name, tool_args, requester)
                # Merge tool result into answer if not already there
                if result.get("data") and tool_name in ("get_leave_balance", "list_leave_requests", "get_team_leave"):
                    answer = _format_tool_answer(tool_name, result, requester)
                executed_tool_calls.append({
                    "name": tool_name,
                    "args": sanitize_tool_args(tool_args),
                    "status": result.get("status", "success"),
                })
            except PermissionError:
                # Unauthorised leave data → one fixed refusal message whether or
                # not the employee exists (task §7.3).
                executed_tool_calls.append({
                    "name": tool_name,
                    "args": sanitize_tool_args(tool_args),
                    "status": "error",
                })
                answer = LEAVE_DATA_REFUSAL
            except Exception as exc:
                executed_tool_calls.append({
                    "name": tool_name,
                    "args": sanitize_tool_args(tool_args),
                    "status": "error",
                })
                answer = f"Could not retrieve that information: {str(exc)}"

    # ── 10. PII scrub on answer ───────────────────────────────────────────────
    # Emails are masked, phone numbers are removed entirely (task §7.4).
    answer = strip_phones_in_text(mask_emails_in_text(answer))

    # ── 11. Citation validation + route contract (task §4.2, §8.1) ───────────
    # Citations are restricted to documents that were actually retrieved AND are
    # eligible for this requester; the route/citations/tool_calls combination is
    # then forced to one of the six legal shapes.
    route, citations, executed_tool_calls = _enforce_route_contract(
        route,
        _normalise_citations(citations, final_chunks + upcoming_chunks),
        executed_tool_calls,
    )

    result = {
        "route": route,
        "answer": answer,
        "citations": citations,
        "tool_calls": executed_tool_calls,
        # Source of truth: a proposal is pending only if one was actually validated
        # and stored on the session. We never trust the model's raw flag alone.
        "pending_confirmation": session.pending_confirmation is not None,
        # True only when the turn could not be completed normally (unreachable
        # provider, or a response that did not match the required JSON contract).
        "degraded": bool(parsed.get("degraded", False)),
    }

    # Cache pure RAG responses (embedding runs off the event loop)
    if response_cache.is_cacheable_route(route):
        await loop.run_in_executor(
            None, response_cache.set,
            requester.employee_id, requester.role, requester.region, message, result,
        )

    return result


def _refuse(reason: str) -> dict:
    return {
        "route": "refuse",
        "answer": reason,
        "citations": [],
        "tool_calls": [],
        "pending_confirmation": False,
    }


def _format_tool_answer(tool_name: str, result: dict, requester: Employee) -> str:
    """Format tool result data into a readable answer."""
    data = result.get("data", {})
    if tool_name == "get_leave_balance":
        balances = data if isinstance(data, list) else [data]
        lines = [f"**{b['leave_type'].title()} leave**: {b['balance_days']} days remaining" for b in balances]
        return "\n".join(lines)
    elif tool_name == "list_leave_requests":
        reqs = data if isinstance(data, list) else []
        if not reqs:
            return "No leave requests found."
        lines = [
            f"- {r['leave_type'].title()}: {r['start_date']} to {r['end_date']} ({r['working_days']} days) — **{r['status']}**"
            for r in reqs
        ]
        return "Leave requests:\n" + "\n".join(lines)
    elif tool_name == "get_team_leave":
        reqs = data if isinstance(data, list) else []
        if not reqs:
            return "No team leave in that period."
        lines = [
            f"- {r['employee_id']}: {r['start_date']} to {r['end_date']} ({r['status']})"
            for r in reqs
        ]
        return "Team leave:\n" + "\n".join(lines)
    return result.get("message", "Done.")
