"""
src/main.py  —  FastAPI application entry point (complete implementation).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config import get_settings
from src.data_access.employee_repo import Employee
from src.orchestration.session import session_store
from src.security.identity import resolve_identity
from src.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

ingestion_state: dict[str, Any] = {
    "ready": False, "docs_ingested": 0, "chunks": 0,
    "started_at": None, "finished_at": None, "error": None, "integrity": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ingestion_state["started_at"] = time.time()
    logger.info("Veltrix PeopleOps Assistant starting")

    if get_settings().skip_ingestion:
        # Test / API-only mode: no vector index is built. ingestion_state is left
        # untouched so tests control readiness explicitly.
        logger.info("Ingestion skipped (SKIP_INGESTION=true)")
    else:
        # Run ingestion in a thread (blocking I/O — keep event loop free)
        loop = asyncio.get_event_loop()
        try:
            from src.ingestion.ingestor import run_ingestion
            await loop.run_in_executor(None, run_ingestion, ingestion_state)
        except Exception as exc:
            logger.error("Ingestion failed", extra={"error": str(exc)})
            ingestion_state["error"] = str(exc)

    yield

    logger.info("Veltrix PeopleOps Assistant shutting down")
    # Persist any cached answers that have not been flushed yet.
    try:
        from src.orchestration.cache import response_cache
        response_cache.save()
    except Exception as exc:  # noqa: BLE001 — shutdown must not raise
        logger.warning("Could not flush semantic cache", extra={"error": str(exc)})
    from src.data_access.db import close_connection
    close_connection()


app = FastAPI(
    title="Veltrix PeopleOps Assistant",
    description="HR chat assistant — policy Q&A + leave management",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


# ─── Models ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str


class CitationModel(BaseModel):
    doc_id: str
    locator: str


class ToolCallModel(BaseModel):
    name: str
    args: dict[str, Any]
    status: str


class ChatResponse(BaseModel):
    request_id: str
    answer: str
    route: str
    citations: list[CitationModel]
    tool_calls: list[ToolCallModel]
    pending_confirmation: bool
    # True only when the assistant could not reach the LLM and returned a
    # fallback message. Lets callers retry instead of treating the fallback as
    # a real answer.
    degraded: bool = False


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    if not ingestion_state["ready"]:
        return JSONResponse(status_code=503, content={
            "status": "initializing",
            "message": "Ingestion in progress. Please wait.",
            **({"error": ingestion_state["error"]} if ingestion_state["error"] else {}),
        })
    elapsed = None
    if ingestion_state["finished_at"] and ingestion_state["started_at"]:
        elapsed = round(ingestion_state["finished_at"] - ingestion_state["started_at"], 1)
    body: dict[str, Any] = {
        "status": "ok",
        "docs_ingested": ingestion_state["docs_ingested"],
        "chunks": ingestion_state["chunks"],
        "ingestion_time_seconds": elapsed,
    }
    if ingestion_state.get("integrity"):
        body["ingestion_integrity"] = ingestion_state["integrity"]
    try:
        from src.orchestration.cache import response_cache
        body["semantic_cache"] = response_cache.stats_snapshot()
    except Exception:  # noqa: BLE001 — health must never fail on telemetry
        pass
    return JSONResponse(status_code=200, content=body)


@app.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    requester: Employee = Depends(resolve_identity),
):
    request_id = str(uuid.uuid4())
    start_time = time.time()

    logger.info("Chat request", extra={
        "request_id": request_id, "employee_id": requester.employee_id,
        "role": requester.role, "session_id": request.session_id,
    })

    # Validate / create session (raises 403 on hijack)
    session = session_store.get_or_create(request.session_id, requester.employee_id)
    session_store.add_message(session, "user", request.message)

    # Full orchestration. The whole request has a hard ceiling below the 30-second
    # limit required by the task, so a slow provider can never hang the caller.
    from src.orchestration.router import handle_message
    try:
        result = await asyncio.wait_for(
            handle_message(request.message, requester, session, request_id),
            timeout=settings.request_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.error("Request timed out", extra={
            "request_id": request_id, "employee_id": requester.employee_id,
            "timeout_seconds": settings.request_timeout_seconds,
        })
        result = {
            "route": "no_answer",
            "answer": "That request took too long to process. Please try again.",
            "citations": [],
            "tool_calls": [],
            "pending_confirmation": False,
            "degraded": True,
        }

    session_store.add_message(session, "assistant", result.get("answer", ""))

    latency_ms = round((time.time() - start_time) * 1000)
    logger.info("Chat response", extra={
        "request_id": request_id, "employee_id": requester.employee_id,
        "route": result.get("route"), "latency_ms": latency_ms,
    })

    return ChatResponse(
        request_id=request_id,
        answer=result.get("answer", ""),
        route=result.get("route", "no_answer"),
        citations=[CitationModel(**c) for c in result.get("citations", [])
                   if isinstance(c, dict) and "doc_id" in c and "locator" in c],
        tool_calls=[ToolCallModel(**tc) for tc in result.get("tool_calls", [])
                    if isinstance(tc, dict) and "name" in tc],
        pending_confirmation=result.get("pending_confirmation", False),
        degraded=bool(result.get("degraded", False)),
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Malformed / missing request body → 400 (task §9), not FastAPI's default 422."""
    logger.warning("Invalid request body", extra={"errors": exc.errors()})
    return JSONResponse(
        status_code=400,
        content={"detail": "Invalid request body. 'session_id' and 'message' are required strings."},
    )


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    logger.warning("HTTP error", extra={"status_code": exc.status_code})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    logger.error("Unhandled error", extra={"error": str(exc)})
    return JSONResponse(status_code=503, content={"detail": "Internal server error. Please try again."})
