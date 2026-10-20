"""Quac ingestion API — FastAPI core engine.

Endpoints
---------
* ``POST /v1/quantum/simulate`` — validate a circuit through the firewall,
  persist a job record, enqueue it onto the SQS FIFO queue and answer with
  ``202 Accepted`` plus a fresh UUIDv4 ``job_id``.
* ``POST /v1/quantum/analyze`` — synchronous pre-flight analysis (exact
  metrics, labelled estimates, warnings, backend recommendation) with zero
  side effects: nothing is persisted or enqueued.
* ``GET  /v1/quantum/jobs/{job_id}`` — job tracking, guarded by a global
  sliding-window rate limiter shared across every route in the process.
* ``GET  /v1/quantum/jobs/{job_id}/result`` — terminal-state result payload
  plus a short-lived pre-signed download URL for the full artifact.
* ``POST /v1/quantum/jobs/{job_id}/cancel`` — atomically cancel a job that
  has not yet reached a terminal state.
* ``GET  /v1/quantum/formats`` — supported formats and platform ceilings.
* ``GET  /healthz`` — liveness probe for the ALB target group.

All blocking I/O (DynamoDB, S3, SQS) is executed off the event loop. Static
circuit validation — including the 28-qubit infrastructure cost firewall —
runs *before* any side effect is created, so hostile payloads are rejected
instantly with ``422`` and never reach the compute plane.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from . import __version__, database, queue_service, results_service
from .schemas import (
    AnalysisRequest,
    CircuitAnalysisResponse,
    CircuitFormat,
    CircuitValidationError,
    JobCancelResponse,
    JobResultPayload,
    JobResultResponse,
    JobStatus,
    JobStatusResponse,
    QubitLimitExceededError,
    SimulationAcceptedResponse,
    SimulationRequest,
    validate_circuit_payload,
)

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("QUAC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("quac.api")

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

RATE_LIMIT_REQUESTS: Final[int] = int(os.getenv("QUAC_RATE_LIMIT_REQUESTS", "120"))
RATE_LIMIT_WINDOW_SECONDS: Final[float] = float(os.getenv("QUAC_RATE_LIMIT_WINDOW", "60"))
MAX_TRACKING_ERROR_CHARS: Final[int] = 2048

# Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY -> ..._CONTENT; prefer the
# new name and fall back to the literal so neither import path warns.
HTTP_422: Final[int] = int(getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422))


def _uuid4() -> str:
    """Return a lowercase UUIDv4 string."""
    return str(uuid.uuid4())


# --------------------------------------------------------------------------------------
# Global sliding-window rate limiter
# --------------------------------------------------------------------------------------


class SlidingWindowRateLimiter:
    """Per-client fixed-capacity sliding-window limiter.

    Deployed as a *global* FastAPI dependency so that every protected route
    (currently the job tracking endpoint) draws from the same limiter instance
    within a process. Stale timestamps are pruned on access, and an asyncio
    lock serialises admissions so concurrent requests cannot over-admit past
    the window cap.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> tuple[bool, float]:
        """Record a hit for ``key``; return ``(allowed, retry_after_seconds)``."""
        now = time.monotonic()
        async with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] >= self._window:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                oldest = bucket[0]
                retry_after = max(0.0, self._window - (now - oldest))
                return False, retry_after
            bucket.append(now)
            return True, 0.0

    async def reset(self) -> None:
        """Clear all recorded hits (used by tests)."""
        async with self._lock:
            self._hits.clear()


rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def _client_key(request: Request) -> str:
    """Prefer the earliest untrusted forwarding hop, else the socket peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


async def rate_limit(request: Request, response: Response) -> None:
    """FastAPI dependency enforcing the global tracking rate limit."""
    allowed, retry_after = await rate_limiter.check(_client_key(request))
    if not allowed:
        header_value = f"{retry_after:.0f}"
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "RATE_LIMIT_EXCEEDED",
                "message": (
                    f"limit is {RATE_LIMIT_REQUESTS} requests "
                    f"per {RATE_LIMIT_WINDOW_SECONDS:.0f}s"
                ),
                "retry_after_seconds": round(retry_after, 3),
            },
            headers={"Retry-After": header_value},
        )


# --------------------------------------------------------------------------------------
# Application lifespan
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the DynamoDB / SQS connection lifecycle for the process."""
    database.initialise(
        table_name=os.getenv("QUAC_JOBS_TABLE"),
        region=os.getenv("QUAC_AWS_REGION") or None,
    )
    logger.info(
        "quac api starting version=%s rate_limit=%d/%.0fs",
        __version__,
        RATE_LIMIT_REQUESTS,
        RATE_LIMIT_WINDOW_SECONDS,
    )
    try:
        yield
    finally:
        queue_service.shutdown()
        results_service.shutdown()
        database.shutdown()
        logger.info("quac api stopped")


app = FastAPI(
    title="Quac — Quantum Simulation MaaS API",
    description=(
        "Serverless Quantum Simulation Model-as-a-Service. Analyze circuits "
        "pre-flight (exact metrics, labelled estimates, backend recommendation), "
        "then submit OpenQASM 3.0 or serialized Qiskit/Cirq circuits for "
        "asynchronous execution and receive signed result callbacks via webhook "
        "— or fetch results directly from the result endpoint."
    ),
    version=__version__,
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "simulation",
            "description": (
                "Circuit submission, job tracking, result retrieval and cancellation. "
                "All long-running work is asynchronous: submit returns a job id, "
                "then poll the tracking endpoint (or receive a webhook)."
            ),
        },
        {
            "name": "analysis",
            "description": (
                "Synchronous pre-flight circuit analysis: exact structural metrics, "
                "clearly labelled estimates, interaction-graph proxies, warnings and "
                "the backend recommendation. No job is created."
            ),
        },
        {
            "name": "ops",
            "description": "Liveness and operational probes.",
        },
    ],
)


# --------------------------------------------------------------------------------------
# Error handlers
# --------------------------------------------------------------------------------------


@app.exception_handler(CircuitValidationError)
async def _circuit_validation_handler(
    request: Request, exc: CircuitValidationError
) -> JSONResponse:
    """Map firewall rejections onto structured 422 responses."""
    detail: dict[str, Any] = {"error": exc.code, "message": str(exc)}
    if isinstance(exc, QubitLimitExceededError):
        detail["observed_qubits"] = exc.observed
        detail["max_qubits"] = exc.limit
    if exc.line is not None:
        detail["line"] = exc.line
    logger.warning("firewall rejected code=%s path=%s", exc.code, request.url.path)
    return JSONResponse(
        status_code=HTTP_422,
        content={"detail": detail},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalise FastAPI HTTP errors into a consistent envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


@app.get("/healthz", tags=["ops"], summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Cheap liveness signal for the ALB target group and ECS health checks."""
    return {"status": "ok", "service": "quac-api", "version": __version__}


@app.post(
    "/v1/quantum/simulate",
    response_model=SimulationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["simulation"],
    summary="Submit a circuit for asynchronous simulation",
    responses={
        202: {"description": "Job accepted and enqueued"},
        422: {"description": "Validation firewall rejection"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Queue or store unavailable"},
    },
)
async def submit_simulation(request_body: SimulationRequest) -> SimulationAcceptedResponse:
    """Validate, persist and enqueue a simulation job.

    The order of operations is deliberate: *all* static validation happens
    before the first write, so rejected payloads have zero infrastructure
    footprint.
    """
    qubit_count = validate_circuit_payload(request_body.circuit_data, request_body.format)
    webhook_url = str(request_body.webhook_url) if request_body.webhook_url else None

    job_id = _uuid4()
    payload_json = json.dumps(
        {
            "format": request_body.format.value,
            "shots": request_body.shots,
            "qubit_count": qubit_count,
            "webhook_url": webhook_url,
            "idempotency_key": request_body.idempotency_key,
        },
        separators=(",", ":"),
    )

    try:
        await database.create_job_record(
            job_id,
            payload_json,
            circuit_format=request_body.format,
            shots=request_body.shots,
            webhook_url=webhook_url,
            qubit_count=qubit_count,
            idempotency_key=request_body.idempotency_key,
        )
    except FileExistsError:
        logger.error("uuid collision on job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "JOB_ID_COLLISION", "message": "retry the request"},
        ) from None
    except Exception as exc:
        logger.exception("failed to persist job record job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "PERSISTENCE_UNAVAILABLE", "message": "job store unreachable"},
        ) from exc

    try:
        await queue_service.enqueue_simulation(job_id, request_body, qubit_count)
    except queue_service.QueuePublishError as exc:
        logger.error("enqueue failed job_id=%s; marking failed", job_id)
        try:
            await database.update_job_status(
                job_id,
                JobStatus.FAILED,
                error_message=f"queue unavailable: {exc}",
            )
        except Exception:
            logger.exception("failed to mark job failed job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "QUEUE_UNAVAILABLE",
                "message": "retry with the same idempotency_key",
            },
        ) from exc

    logger.info(
        "job accepted job_id=%s qubits=%d shots=%d format=%s",
        job_id,
        qubit_count,
        request_body.shots,
        request_body.format.value,
    )
    return SimulationAcceptedResponse(
        job_id=job_id,
        status=JobStatus.QUEUED,
        status_url=f"/v1/quantum/jobs/{job_id}",
    )


@app.post(
    "/v1/quantum/analyze",
    response_model=CircuitAnalysisResponse,
    tags=["analysis"],
    summary="Analyze a circuit before submitting it",
    dependencies=[Depends(rate_limit)],
    responses={
        200: {"description": "Pre-flight analysis report"},
        422: {"description": "Validation firewall rejection or unparseable circuit"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def analyze_circuit(request_body: AnalysisRequest) -> CircuitAnalysisResponse:
    """Compute the pre-flight report for a circuit — synchronously.

    The firewall runs first (identical rules to simulation ingestion), then
    the shared ``core`` analyzer derives exact structural metrics, labelled
    estimates and the backend recommendation. This endpoint has **zero side
    effects**: no job record, no queue message, no object storage write.

    The qiskit/cirq import is deferred to the first call so process boot and
    ``/healthz`` stay fast on cold starts.
    """
    validate_circuit_payload(request_body.circuit_data, request_body.format)

    # Deferred imports: qiskit/cirq are heavy; keep them off the boot path so
    # process start and /healthz stay fast on cold starts.
    from core import analysis as core_analysis
    from core.circuit import CircuitBuildError

    try:
        report = await asyncio.to_thread(
            core_analysis.analyze,
            request_body.circuit_data,
            request_body.format.value,
            request_body.shots,
        )
    except CircuitBuildError as exc:
        # The firewall passed but the parser still refused the payload: a
        # client-side circuit defect, mapped onto the standard 422 envelope.
        logger.info("analysis build failed format=%s error=%s", request_body.format.value, exc)
        raise HTTPException(
            status_code=HTTP_422,
            detail={"error": "CIRCUIT_ANALYSIS_FAILED", "message": str(exc)},
        ) from exc

    return CircuitAnalysisResponse.model_validate(report)


async def _fetch_job_record(job_id: str) -> dict[str, Any]:
    """Resolve a path job id to its record, mapping misses onto 404/503.

    Centralised so every job-scoped route applies identical id-shape checks
    (strict UUIDv4) and identical failure envelopes.
    """
    try:
        parsed = uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "JOB_NOT_FOUND", "message": f"unknown job id {job_id!r}"},
        ) from exc
    if parsed.version != 4:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "JOB_NOT_FOUND", "message": f"unknown job id {job_id!r}"},
        )

    try:
        return await database.get_job_record(job_id)
    except database.JobRecordNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "JOB_NOT_FOUND", "message": f"unknown job id {job_id!r}"},
        ) from exc
    except Exception as exc:
        logger.exception("job lookup failed job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "PERSISTENCE_UNAVAILABLE", "message": "job store unreachable"},
        ) from exc


def _truncate_error(message: str | None) -> str | None:
    """Bound stored diagnostics so tracking payloads stay small."""
    if isinstance(message, str) and len(message) > MAX_TRACKING_ERROR_CHARS:
        return message[:MAX_TRACKING_ERROR_CHARS] + "…[truncated]"
    return message


@app.get(
    "/v1/quantum/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["simulation"],
    summary="Track a simulation job",
    dependencies=[Depends(rate_limit)],
    responses={
        404: {"description": "Unknown job id"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def get_job(job_id: str) -> JobStatusResponse:
    """Return the current lifecycle state of a previously submitted job."""
    record = await _fetch_job_record(job_id)

    return JobStatusResponse(
        job_id=str(record["job_id"]),
        status=JobStatus(record.get("status", JobStatus.QUEUED.value)),
        created_at=str(record.get("created_at", "")),
        completed_at=record.get("completed_at"),
        error_message=_truncate_error(record.get("error_message")),
    )


@app.get(
    "/v1/quantum/jobs/{job_id}/result",
    response_model=JobResultResponse,
    tags=["simulation"],
    summary="Fetch a terminal job's result",
    dependencies=[Depends(rate_limit)],
    responses={
        404: {"description": "Unknown job id"},
        409: {"description": "Job has not reached a terminal state yet"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Result artifact store unavailable"},
    },
)
async def get_job_result(job_id: str) -> JobResultResponse:
    """Return the result payload for a terminal job.

    ``409`` while the job is still queued or processing; ``200`` with
    ``result: null`` for failed/cancelled jobs; ``200`` with the counts and a
    pre-signed download URL for completed jobs.
    """
    record = await _fetch_job_record(job_id)
    job_state = JobStatus(record.get("status", JobStatus.QUEUED.value))

    if not job_state.is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "JOB_NOT_READY",
                "message": f"job is {job_state.value}; results arrive at a terminal state",
                "status": job_state.value,
            },
        )

    if job_state is not JobStatus.COMPLETED:
        return JobResultResponse(
            job_id=str(record["job_id"]),
            status=job_state,
            error_message=_truncate_error(record.get("error_message")),
        )

    result_key = record.get("result_s3_key")
    if not isinstance(result_key, str) or not result_key:
        logger.error("completed job without result artifact job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "RESULT_UNAVAILABLE", "message": "result artifact not recorded"},
        )

    try:
        document = await results_service.fetch_result_document(result_key)
        download_url = await results_service.presign_result_url(result_key)
    except results_service.ResultNotFound:
        logger.error("result artifact missing job_id=%s key=%s", job_id, result_key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "RESULT_UNAVAILABLE", "message": "result artifact not found"},
        ) from None
    except results_service.ResultUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "RESULT_UNAVAILABLE", "message": str(exc)},
        ) from exc

    try:
        payload = JobResultPayload(
            shots=int(document["shots"]),
            counts={str(k): int(v) for k, v in dict(document["counts"]).items()},
            most_frequent=document.get("most_frequent"),
            method=str(document.get("method", "unknown")),
            backend=str(document.get("backend", "")),
            selection_reasons=[
                str(reason) for reason in (document.get("selection_reasons") or [])
            ],
            num_qubits=int(document.get("num_qubits", 0)),
            depth=int(document.get("depth", 0)),
            simulation_time_ms=document.get("simulation_time_ms"),
            # Dense statevector maps stay behind the download URL (size ceiling).
            statevector_probabilities=document.get("statevector_probabilities"),
            metadata=dict(document.get("metadata") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("result document malformed job_id=%s error=%s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "RESULT_UNAVAILABLE", "message": "result artifact malformed"},
        ) from exc

    return JobResultResponse(
        job_id=str(record["job_id"]),
        status=job_state,
        result=payload,
        download_url=download_url,
        download_expires_in_seconds=results_service.PRESIGNED_URL_TTL_SECONDS,
    )


@app.post(
    "/v1/quantum/jobs/{job_id}/cancel",
    response_model=JobCancelResponse,
    tags=["simulation"],
    summary="Cancel a job that has not finished",
    dependencies=[Depends(rate_limit)],
    responses={
        404: {"description": "Unknown job id"},
        409: {"description": "Job already reached a terminal state"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Job store unavailable"},
    },
)
async def cancel_job(job_id: str) -> JobCancelResponse:
    """Cancel a queued or running job.

    The transition is conditional in the store: a job that finishes while the
    cancel request is in flight is *not* overwritten — the request fails with
    ``409`` instead. A worker that is already computing observes the cancelled
    status at its next checkpoint and discards the late result.
    """
    try:
        parsed_id = uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "JOB_NOT_FOUND", "message": f"unknown job id {job_id!r}"},
        ) from exc
    if parsed_id.version != 4:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "JOB_NOT_FOUND", "message": f"unknown job id {job_id!r}"},
        )

    try:
        record = await database.cancel_job_record(job_id)
    except database.JobRecordNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "JOB_NOT_FOUND", "message": f"unknown job id {job_id!r}"},
        ) from exc
    except database.JobNotCancellableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "JOB_NOT_CANCELLABLE",
                "message": "job already reached a terminal state",
                "current_status": exc.current_status,
            },
        ) from exc
    except Exception as exc:
        logger.exception("cancel failed job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "PERSISTENCE_UNAVAILABLE", "message": "job store unreachable"},
        ) from exc

    logger.info("cancel accepted job_id=%s", job_id)
    return JobCancelResponse(
        job_id=str(record.get("job_id", job_id)),
        status=JobStatus(record.get("status", JobStatus.CANCELLED.value)),
    )


@app.get("/v1/quantum/formats", tags=["simulation"], summary="List accepted circuit formats")
async def list_formats() -> dict[str, Any]:
    """Advertise the supported serialization formats and the qubit ceiling."""
    return {
        "formats": [member.value for member in CircuitFormat],
        "max_qubits": 28,
        "max_shots": 8192,
    }


__all__ = [
    "CircuitFormat",
    "SlidingWindowRateLimiter",
    "app",
    "rate_limit",
    "rate_limiter",
]
