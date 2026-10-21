"""Quac ingestion API — FastAPI core engine.

Endpoints
---------
* ``POST /v1/quantum/simulate`` — validate a circuit through the firewall,
  enforce plan limits and quotas, persist a job record, enqueue it onto the
  SQS FIFO queue and answer with ``202 Accepted`` plus a fresh UUIDv4
  ``job_id``.
* ``POST /v1/quantum/analyze`` — synchronous pre-flight analysis (exact
  metrics, labelled estimates, warnings, backend recommendation) with zero
  side effects: nothing is persisted or enqueued.
* ``GET  /v1/quantum/jobs`` — the caller's own jobs, newest first
  (owner-scoped, cursor-paginated).
* ``GET  /v1/quantum/jobs/{job_id}`` — job tracking, guarded by a global
  sliding-window rate limiter shared across every route in the process.
* ``GET  /v1/quantum/jobs/{job_id}/result`` — terminal-state result payload
  plus a short-lived pre-signed download URL for the full artifact.
* ``POST /v1/quantum/jobs/{job_id}/cancel`` — atomically cancel a job that
  has not yet reached a terminal state.
* ``GET  /v1/quantum/formats`` — supported formats and platform ceilings.
* ``GET  /healthz`` — liveness probe for the ALB target group.
* ``/v1/auth/*`` and ``/v1/keys`` — registration, session login and API-key
  management (see :mod:`api.auth_routes`).

All ``/v1/quantum/*`` routes except ``/v1/quantum/formats`` require an
``X-API-Key`` header or the ``quac_session`` cookie (see
:func:`api.auth.require_identity`). Cross-tenant job ids read as ``404``.

All blocking I/O (DynamoDB, S3, SQS) is executed off the event loop. Static
circuit validation — including the 28-qubit infrastructure cost firewall —
runs *before* any side effect is created, so hostile payloads are rejected
instantly with ``422`` and never reach the compute plane.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from . import __version__, auth, database, identity, queue_service, results_service
from .auth import Identity, require_identity
from .auth_routes import router as auth_router
from .ratelimit import (
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    SlidingWindowRateLimiter,
    rate_limit,
    rate_limiter,
)
from .schemas import (
    AnalysisRequest,
    CircuitAnalysisResponse,
    CircuitFormat,
    CircuitValidationError,
    JobCancelResponse,
    JobListItem,
    JobListResponse,
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
# The limiter implementation moved to ``api.ratelimit`` (shared with the
# per-identity plan limiter). The names are re-exported here so existing
# ``from api.main import rate_limiter`` call sites keep working.


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
        {
            "name": "auth",
            "description": (
                "Registration, session login and API-key management. Every /v1/quantum "
                "route requires either an X-API-Key header or a session cookie."
            ),
        },
    ],
)

app.include_router(auth_router)


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
        401: {"description": "Missing or invalid credentials"},
        403: {"description": "Plan limit exceeded (qubits/shots)"},
        422: {"description": "Validation firewall rejection"},
        429: {"description": "Rate limit or quota exceeded"},
        503: {"description": "Queue or store unavailable"},
    },
)
async def submit_simulation(
    request_body: SimulationRequest,
    principal: Annotated[Identity, Depends(require_identity)],
) -> SimulationAcceptedResponse:
    """Validate, persist and enqueue a simulation job.

    The order of operations is deliberate: *all* static validation happens
    before the first write, so rejected payloads have zero infrastructure
    footprint. Admission gates then run cheapest-first (plan ceilings, then
    concurrency, then the monthly counter) so quota-limited submits fail
    before touching the job store.
    """
    qubit_count = validate_circuit_payload(request_body.circuit_data, request_body.format)
    webhook_url = str(request_body.webhook_url) if request_body.webhook_url else None

    plan = principal.plan
    # Plan ceilings run after the platform firewall: a circuit that violates
    # the 28-qubit platform cap is a 422, one that exceeds the *plan's* width
    # or shot budget is a 403.
    if qubit_count > plan.max_qubits or request_body.shots > plan.max_shots:
        auth.schedule_audit(
            principal.user_id,
            "plan.limit_exceeded",
            {
                "observed_qubits": qubit_count,
                "plan_max_qubits": plan.max_qubits,
                "observed_shots": request_body.shots,
                "plan_max_shots": plan.max_shots,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "PLAN_LIMIT_EXCEEDED",
                "message": (
                    f"plan '{plan.name}' allows up to {plan.max_qubits} qubits "
                    f"and {plan.max_shots} shots"
                ),
                "observed_qubits": qubit_count,
                "plan_max_qubits": plan.max_qubits,
                "observed_shots": request_body.shots,
                "plan_max_shots": plan.max_shots,
            },
        )

    # Monthly compute admission: recorded usage only — in-flight jobs may
    # overshoot slightly (documented trade-off vs. serialising every submit).
    month = identity.current_month()
    usage_row = await identity.get_usage(principal.user_id, month)
    compute_used = int(usage_row.get("compute_ms", 0)) if usage_row else 0
    if compute_used >= plan.monthly_compute_ms:
        auth.schedule_audit(
            principal.user_id,
            "quota.exceeded",
            {"quota": "compute_ms", "used": compute_used, "limit": plan.monthly_compute_ms},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "QUOTA_EXCEEDED",
                "message": f"plan '{plan.name}' monthly compute budget exhausted",
                "quota": "compute_ms",
                "used_ms": compute_used,
                "limit_ms": plan.monthly_compute_ms,
                "month": month,
            },
        )

    # Concurrent-job cap (soft: the owner GSI is eventually consistent).
    active = await database.count_active_jobs(principal.user_id)
    if active >= plan.max_concurrent_jobs:
        auth.schedule_audit(
            principal.user_id,
            "quota.exceeded",
            {"quota": "concurrent_jobs", "used": active, "limit": plan.max_concurrent_jobs},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "TOO_MANY_ACTIVE_JOBS",
                "message": (
                    f"plan '{plan.name}' allows {plan.max_concurrent_jobs} "
                    "queued/processing jobs"
                ),
                "active_jobs": active,
                "limit": plan.max_concurrent_jobs,
            },
        )

    # Atomic monthly request counter (conditional ADD — race-safe).
    try:
        await identity.reserve_request_quota(principal.user_id, month, plan.monthly_requests)
    except identity.QuotaExceededError:
        auth.schedule_audit(
            principal.user_id,
            "quota.exceeded",
            {"quota": "requests", "limit": plan.monthly_requests, "month": month},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "QUOTA_EXCEEDED",
                "message": f"plan '{plan.name}' monthly request quota exhausted",
                "quota": "requests",
                "limit": plan.monthly_requests,
                "month": month,
            },
        ) from None

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
            owner_id=principal.user_id,
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
        401: {"description": "Missing or invalid credentials"},
        422: {"description": "Validation firewall rejection or unparseable circuit"},
        429: {"description": "Rate limit or quota exceeded"},
    },
)
async def analyze_circuit(
    request_body: AnalysisRequest,
    principal: Annotated[Identity, Depends(require_identity)],
) -> CircuitAnalysisResponse:
    """Compute the pre-flight report for a circuit — synchronously.

    The firewall runs first (identical rules to simulation ingestion), then
    the shared ``core`` analyzer derives exact structural metrics, labelled
    estimates and the backend recommendation. This endpoint has **zero side
    effects**: no job record, no queue message, no object storage write.
    Analysis counts against the monthly *request* quota (it is real work for
    the platform) but is not gated on the plan's qubit/shot ceilings — it
    exists precisely to let users check circuits before submitting.

    The qiskit/cirq import is deferred to the first call so process boot and
    ``/healthz`` stay fast on cold starts.
    """
    validate_circuit_payload(request_body.circuit_data, request_body.format)

    plan = principal.plan
    month = identity.current_month()
    try:
        await identity.reserve_request_quota(principal.user_id, month, plan.monthly_requests)
    except identity.QuotaExceededError:
        auth.schedule_audit(
            principal.user_id,
            "quota.exceeded",
            {"quota": "requests", "limit": plan.monthly_requests, "month": month},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "QUOTA_EXCEEDED",
                "message": f"plan '{plan.name}' monthly request quota exhausted",
                "quota": "requests",
                "limit": plan.monthly_requests,
                "month": month,
            },
        ) from None

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


def _job_not_found(job_id: str) -> HTTPException:
    """The single 404 envelope for unknown *and* foreign-tenant job ids.

    Cross-tenant reads return the same shape as a missing id so job
    existence is never confirmed across tenant boundaries.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "JOB_NOT_FOUND", "message": f"unknown job id {job_id!r}"},
    )


async def _fetch_job_record(job_id: str, owner_id: str) -> dict[str, Any]:
    """Resolve a path job id to the caller's record, mapping misses onto 404/503.

    Centralised so every job-scoped route applies identical id-shape checks
    (strict UUIDv4), identical ownership enforcement (cross-tenant rows read
    as 404) and identical failure envelopes.
    """
    try:
        parsed = uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise _job_not_found(job_id) from exc
    if parsed.version != 4:
        raise _job_not_found(job_id)

    try:
        record = await database.get_job_record(job_id)
    except database.JobRecordNotFoundError as exc:
        raise _job_not_found(job_id) from exc
    except Exception as exc:
        logger.exception("job lookup failed job_id=%s", job_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "PERSISTENCE_UNAVAILABLE", "message": "job store unreachable"},
        ) from exc

    if str(record.get("owner_id", "")) != owner_id:
        # Foreign tenant (or an unowned legacy row): indistinguishable from a
        # missing id, so job existence never leaks across tenants.
        raise _job_not_found(job_id)
    return record


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
        401: {"description": "Missing or invalid credentials"},
        404: {"description": "Unknown job id (or another tenant's)"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def get_job(
    job_id: str, principal: Annotated[Identity, Depends(require_identity)]
) -> JobStatusResponse:
    """Return the current lifecycle state of a previously submitted job."""
    record = await _fetch_job_record(job_id, principal.user_id)

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
        401: {"description": "Missing or invalid credentials"},
        404: {"description": "Unknown job id (or another tenant's)"},
        409: {"description": "Job has not reached a terminal state yet"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Result artifact store unavailable"},
    },
)
async def get_job_result(
    job_id: str, principal: Annotated[Identity, Depends(require_identity)]
) -> JobResultResponse:
    """Return the result payload for a terminal job.

    ``409`` while the job is still queued or processing; ``200`` with
    ``result: null`` for failed/cancelled jobs; ``200`` with the counts and a
    pre-signed download URL for completed jobs.
    """
    record = await _fetch_job_record(job_id, principal.user_id)
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
        401: {"description": "Missing or invalid credentials"},
        404: {"description": "Unknown job id (or another tenant's)"},
        409: {"description": "Job already reached a terminal state"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Job store unavailable"},
    },
)
async def cancel_job(
    job_id: str, principal: Annotated[Identity, Depends(require_identity)]
) -> JobCancelResponse:
    """Cancel a queued or running job.

    The transition is conditional in the store: a job that finishes while the
    cancel request is in flight is *not* overwritten — the request fails with
    ``409`` instead. A worker that is already computing observes the cancelled
    status at its next checkpoint and discards the late result. Ownership is
    confirmed first so a foreign job id reads as ``404`` before any write.
    """
    await _fetch_job_record(job_id, principal.user_id)

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


def _encode_cursor(last_evaluated_key: dict[str, Any] | None) -> str | None:
    """Encode a GSI page boundary as an opaque base64 cursor."""
    if not last_evaluated_key:
        return None
    raw = json.dumps(last_evaluated_key, separators=(",", ":"), default=str)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    """Decode a client cursor; ``400`` when it is not one we emitted."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_CURSOR", "message": "cursor is not a valid page token"},
        ) from exc
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "INVALID_CURSOR", "message": "cursor is not a valid page token"},
        )
    return decoded


@app.get(
    "/v1/quantum/jobs",
    response_model=JobListResponse,
    tags=["simulation"],
    summary="List the caller's jobs (newest first)",
    dependencies=[Depends(rate_limit)],
    responses={
        400: {"description": "Malformed pagination cursor"},
        401: {"description": "Missing or invalid credentials"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def list_jobs(
    principal: Annotated[Identity, Depends(require_identity)],
    job_status: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: int = 20,
    cursor: str | None = None,
) -> JobListResponse:
    """Page through the caller's own jobs, newest first.

    Reads run off the ``owner_id-index`` GSI and filter on ``entity`` so
    identity rows sharing the owner never appear. ``next_cursor`` is opaque;
    pass it back verbatim for the next page (``null`` when exhausted).
    """
    exclusive_start_key = _decode_cursor(cursor) if cursor else None
    try:
        items, last_key = await database.list_jobs_by_owner(
            principal.user_id,
            status=job_status.value if job_status else None,
            limit=max(1, min(limit, 100)),
            exclusive_start_key=exclusive_start_key,
        )
    except Exception as exc:
        logger.exception("job list failed owner=%s", principal.user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "PERSISTENCE_UNAVAILABLE", "message": "job store unreachable"},
        ) from exc

    return JobListResponse(
        jobs=[
            JobListItem(
                job_id=str(item.get("job_id", "")),
                status=JobStatus(item.get("status", JobStatus.QUEUED.value)),
                created_at=str(item.get("created_at", "")),
                completed_at=item.get("completed_at"),
                format=str(item.get("format", "")),
                shots=int(item.get("shots", 0) or 0),
                qubit_count=int(item.get("qubit_count", 0) or 0),
                error_message=_truncate_error(item.get("error_message")),
            )
            for item in items
        ],
        next_cursor=_encode_cursor(last_key),
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
    "Identity",
    "SlidingWindowRateLimiter",
    "app",
    "rate_limit",
    "rate_limiter",
    "require_identity",
]
