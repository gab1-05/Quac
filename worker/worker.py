"""Quac worker — AWS Lambda SQS consumer and result dispatcher.

Challenge #1 · Cold-start penalty
---------------------------------
All heavyweight runtime packages (``qiskit``, ``qiskit_aer``, ``numpy`` via
:mod:`worker.simulator`, plus ``boto3`` and ``httpx``) are imported at module
scope. The Lambda container runtime imports this module during the
*initialization* phase — outside the active handler loop — so C++ extension
linkage is amortised across invocations instead of being billed per request.

Challenge #3 · Graceful timeout deflection
------------------------------------------
The Lambda function is configured with the maximum 10-minute (600 s) timeout.
A POSIX ``SIGALRM`` interval timer is armed at :data:`TIMEOUT_GUARD_SECONDS`
(570 s = 9:30) for every record. The simulator runs on a daemon compute
thread while the main thread waits; when the alarm fires, the signal handler
raises :class:`SimulationTimeoutError` *in the main thread*, which:

1. writes a ``Timeout Error`` diagnostic into DynamoDB (status ``failed``),
2. fires the consumer webhook with the failure summary,
3. clears the timer and completes the SQS record cleanly.

The orphaned compute thread is a daemon and is reaped when the execution
context freezes — critically, the message is already completed, so it never
enters an endless SQS retry cycle. A secondary guard derived from
``context.get_remaining_time_in_millis()`` backs up the alarm so the
runtime never hits the hard 10-minute kill.

Failure taxonomy
----------------
Business failures (parse errors, OOM, simulation errors, timeouts) are
recorded, webhook-notified, and *swallowed* — the SQS record is completed.
Cancellation is also a business outcome: a job cancelled between dequeue and
execution (or while finalising) is discarded with a ``simulation.cancelled``
callback and the message is acknowledged. Only infrastructure failures
(DynamoDB/S3 client errors) surface as ``batchItemFailures`` so AWS can retry
those specific records.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import signal
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

try:  # Lambda container loads this package as ``worker.*``; local runs differ.
    from .simulator import (
        SimulationBuildError,
        SimulationExecutionError,
        SimulationResult,
        run_simulation,
    )
except ImportError:  # pragma: no cover - flat-import fallback
    from simulator import (  # type: ignore[no-redef]
        SimulationBuildError,
        SimulationExecutionError,
        SimulationResult,
        run_simulation,
    )

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("QUAC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("quac.worker")

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

TIMEOUT_GUARD_SECONDS: Final[int] = int(os.getenv("QUAC_TIMEOUT_GUARD_SECONDS", "570"))
"""9 minutes 30 seconds — fires before Lambda's 600 s hard timeout."""

LAMBDA_SAFETY_MARGIN_SECONDS: Final[int] = 30

RESULTS_BUCKET: Final[str] = os.getenv("QUAC_RESULTS_BUCKET", "quac-results")
JOBS_TABLE: Final[str] = os.getenv("QUAC_JOBS_TABLE", "quac-jobs")
AWS_REGION: Final[str] = os.getenv("QUAC_AWS_REGION", os.getenv("AWS_REGION", "us-east-1"))
WEBHOOK_SECRET: Final[str] = os.getenv("QUAC_WEBHOOK_SECRET", "")
PRESIGNED_URL_TTL_SECONDS: Final[int] = int(os.getenv("QUAC_PRESIGNED_TTL", "900"))

WEBHOOK_MAX_ATTEMPTS: Final[int] = 3
WEBHOOK_BASE_DELAY_SECONDS: Final[float] = 0.5
WEBHOOK_MAX_DELAY_SECONDS: Final[float] = 8.0
WEBHOOK_JITTER_RATIO: Final[float] = 0.5
WEBHOOK_TIMEOUT_SECONDS: Final[float] = 10.0

ERROR_TRACE_MAX_CHARS: Final[int] = 4000

_BOTO_CONFIG: Final[BotoConfig] = BotoConfig(
    max_pool_connections=16,
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=3,
    read_timeout=30,
    user_agent_extra="quac-worker/1.0.0",
)


class SimulationTimeoutError(TimeoutError):
    """Raised inside the main thread by the SIGALRM guard at 9:30."""


class InfrastructureError(RuntimeError):
    """Raised for AWS-side failures that should be retried by SQS."""


class JobCancelledError(RuntimeError):
    """The job was cancelled concurrently; the current work must be discarded.

    Raised by guarded status transitions (``unless_cancelled``) whenever the
    stored record has already moved to ``cancelled``. Treated as a *business*
    outcome: the SQS record is acknowledged, no failure is written, and a
    ``simulation.cancelled`` webhook is delivered.
    """


# --------------------------------------------------------------------------------------
# Pooled AWS clients (module scope — reused across warm invocations)
# --------------------------------------------------------------------------------------

_dynamodb_resource: Any | None = None
_s3_client: Any | None = None
_executor: ThreadPoolExecutor | None = None


def _get_dynamodb() -> Any:
    """Lazily create the pooled DynamoDB resource."""
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource(
            "dynamodb", region_name=AWS_REGION, config=_BOTO_CONFIG
        )
    return _dynamodb_resource


def _get_s3() -> Any:
    """Lazily create the pooled S3 client."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION, config=_BOTO_CONFIG)
    return _s3_client


def _get_executor() -> ThreadPoolExecutor:
    """Lazily create the single-flight compute thread pool (daemon threads)."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quac-sim")
    return _executor


# --------------------------------------------------------------------------------------
# Timeout guard (Challenge #3)
# --------------------------------------------------------------------------------------


def _timeout_signal_handler(signum: int, frame: Any) -> None:
    """SIGALRM callback: interrupt the main thread with a typed timeout."""
    del signum, frame
    raise SimulationTimeoutError(
        f"simulation exceeded the {TIMEOUT_GUARD_SECONDS}s internal guard (9:30 of 10:00)"
    )


def _arm_timeout_guard(seconds: int) -> None:
    """Arm the per-invocation interval timer (POSIX SIGALRM)."""
    try:
        signal.signal(signal.SIGALRM, _timeout_signal_handler)
        signal.setitimer(signal.ITIMER_REAL, float(seconds))
    except (ValueError, OSError, AttributeError) as exc:
        # Non-POSIX platforms (local Windows dev) fall back to the future's
        # timeout; the Lambda runtime on Linux always supports setitimer.
        logger.warning("timeout guard unavailable on this platform: %s", exc)


def _disarm_timeout_guard() -> None:
    """Cancel any pending alarm so it cannot leak into the next record."""
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
    except (ValueError, OSError, AttributeError):  # pragma: no cover - platform guard
        logger.debug("could not disarm timeout guard")


def _remaining_budget_seconds(context: Any) -> float:
    """Compute the safe execution budget from the Lambda context, if present."""
    if context is None or not hasattr(context, "get_remaining_time_in_millis"):
        return float(TIMEOUT_GUARD_SECONDS)
    try:
        remaining_ms = float(context.get_remaining_time_in_millis())
    except Exception:  # noqa: BLE001 - defensive: context must never break the loop
        return float(TIMEOUT_GUARD_SECONDS)
    return max(1.0, (remaining_ms / 1000.0) - LAMBDA_SAFETY_MARGIN_SECONDS)


# --------------------------------------------------------------------------------------
# DynamoDB helpers
# --------------------------------------------------------------------------------------


def _update_job_status(
    job_id: str,
    status: str,
    *,
    error_message: str | None = None,
    simulation_time_ms: int | None = None,
    result_s3_key: str | None = None,
    unless_cancelled: bool = False,
) -> None:
    """Transition a job item, raising :class:`InfrastructureError` on AWS faults.

    When ``unless_cancelled`` is set, the update is guarded by a condition that
    refuses to overwrite a ``cancelled`` record; a violation raises
    :class:`JobCancelledError` instead of silently resurrecting the job.
    """
    names: dict[str, str] = {"#status": "status"}
    values: dict[str, Any] = {":status": status}
    updates = ["#status = :status"]
    condition: str | None = None

    if unless_cancelled:
        values[":cancelled"] = "cancelled"
        condition = "#status <> :cancelled"

    if error_message is not None:
        updates.append("error_message = :err")
        values[":err"] = error_message[:ERROR_TRACE_MAX_CHARS]
    if simulation_time_ms is not None:
        updates.append("simulation_time_ms = :ms")
        values[":ms"] = simulation_time_ms
    if result_s3_key is not None:
        updates.append("result_s3_key = :key")
        values[":key"] = result_s3_key
    if status in ("completed", "failed"):
        updates.append("completed_at = :done")
        values[":done"] = datetime.now(UTC).isoformat()

    kwargs: dict[str, Any] = {
        "Key": {"job_id": job_id},
        "UpdateExpression": "SET " + ", ".join(updates),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    if condition is not None:
        kwargs["ConditionExpression"] = condition

    try:
        _get_dynamodb().Table(JOBS_TABLE).update_item(**kwargs)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ConditionalCheckFailedException" and unless_cancelled:
            # Confirm the cancellation before classifying the failure.
            current = _read_job_record(job_id)
            if current is not None and current.get("status") == "cancelled":
                raise JobCancelledError(f"job {job_id} was cancelled") from exc
            # Attribute vanished or another state raced in: treat as infra.
        logger.error("dynamodb update failed job_id=%s error=%s", job_id, exc)
        raise InfrastructureError(f"dynamodb update failed: {exc}") from exc
    except BotoCoreError as exc:
        logger.error("dynamodb update failed job_id=%s error=%s", job_id, exc)
        raise InfrastructureError(f"dynamodb update failed: {exc}") from exc


def _read_job_record(job_id: str) -> dict[str, Any] | None:
    """Read the full job item; ``None`` when missing or unreachable."""
    try:
        response = _get_dynamodb().Table(JOBS_TABLE).get_item(Key={"job_id": job_id})
    except (ClientError, BotoCoreError) as exc:
        logger.warning("dynamodb read failed job_id=%s error=%s", job_id, exc)
        return None
    item = response.get("Item")
    return dict(item) if item else None


def _read_job_webhook_url(job_id: str) -> str | None:
    """Fetch the consumer webhook URL recorded at ingest time."""
    record = _read_job_record(job_id)
    if not record:
        return None
    webhook = record.get("webhook_url")
    return str(webhook) if webhook else None


def _record_usage(job_id: str, compute_ms: int) -> None:
    """Attribute consumed compute time to the job owner's monthly counter.

    Reads the job record for ``owner_id``, then ADDs ``compute_ms`` into the
    owner's ``USAGE#<owner>#<YYYY-MM>`` row. Owners ``system`` (pre-auth legacy
    rows) and missing owners are skipped — there is nobody to bill. Any error
    is swallowed: usage accounting must never fail the SQS acknowledgement or
    retry a completed job.
    """
    record = _read_job_record(job_id)
    owner = str((record or {}).get("owner_id", "") or "")
    if not owner or owner == "system":
        return
    month = datetime.now(UTC).strftime("%Y-%m")
    try:
        _get_dynamodb().Table(JOBS_TABLE).update_item(
            Key={"job_id": f"USAGE#{owner}#{month}"},
            UpdateExpression=(
                "ADD compute_ms :ms "
                "SET owner_id = if_not_exists(owner_id, :owner), "
                "created_at = if_not_exists(created_at, :now), "
                "#e = if_not_exists(#e, :entity), "
                "#month = if_not_exists(#month, :month_value)"
            ),
            ExpressionAttributeNames={"#e": "entity", "#month": "month"},
            ExpressionAttributeValues={
                ":ms": int(compute_ms),
                ":owner": owner,
                ":now": datetime.now(UTC).isoformat(),
                ":entity": "usage",
                ":month_value": month,
            },
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "usage write failed job_id=%s owner=%s error=%s", job_id, owner, exc
        )


# --------------------------------------------------------------------------------------
# S3 result storage
# --------------------------------------------------------------------------------------


def _result_key(now: datetime, job_id: str) -> str:
    """Build the ``results/{year}/{month}/{day}/{job_id}.json`` object key."""
    return f"results/{now.year:04d}/{now.month:02d}/{now.day:02d}/{job_id}.json"


def _upload_result(key: str, document: dict[str, Any]) -> None:
    """Persist the heavy result document to S3 (single-part JSON put)."""
    body = json.dumps(document, separators=(",", ":"), ensure_ascii=True, default=str)
    try:
        _get_s3().put_object(
            Bucket=RESULTS_BUCKET,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
            Metadata={"job-id": str(document.get("job_id", "unknown"))},
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("s3 put failed bucket=%s key=%s error=%s", RESULTS_BUCKET, key, exc)
        raise InfrastructureError(f"s3 upload failed: {exc}") from exc


def _presign_download(key: str) -> str:
    """Issue a time-boxed pre-signed GET URL for the result object."""
    try:
        return str(
            _get_s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": RESULTS_BUCKET, "Key": key},
                ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
            )
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("presign failed key=%s error=%s", key, exc)
        raise InfrastructureError(f"presign failed: {exc}") from exc


def _delete_result_object(key: str) -> None:
    """Best-effort removal of an artifact whose job was cancelled mid-write."""
    try:
        _get_s3().delete_object(Bucket=RESULTS_BUCKET, Key=key)
        logger.info("removed orphaned result object key=%s", key)
    except (ClientError, BotoCoreError) as exc:
        # Lifecycle rules eventually reclaim the object; never fail the ack.
        logger.warning("could not delete orphaned key=%s error=%s", key, exc)


# --------------------------------------------------------------------------------------
# Resilient webhook dispatcher
# --------------------------------------------------------------------------------------


def _sign_payload(body: bytes) -> str:
    """HMAC-SHA256 signature over the exact request body bytes."""
    if not WEBHOOK_SECRET:
        return ""
    digest = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at the configured ceiling.

    ``attempt`` is zero-based for retries only: retry 1 waits ~0.5-0.75s,
    retry 2 waits ~1.0-1.5s (base doubling, scaled by a random jitter factor).
    """
    ceiling = min(WEBHOOK_BASE_DELAY_SECONDS * (2**attempt), WEBHOOK_MAX_DELAY_SECONDS)
    jitter = random.uniform(0.0, WEBHOOK_JITTER_RATIO) * ceiling
    return ceiling + jitter


async def _post_webhook_async(url: str, payload: dict[str, Any]) -> bool:
    """POST a webhook with up to 3 attempts, exponential backoff and jitter.

    Returns ``True`` once any attempt yields a non-error HTTP status; returns
    ``False`` after exhausting attempts. Never raises — webhook delivery is
    best-effort and must not fail the job itself.
    """
    body = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "quac-worker/1.0.0",
        "X-Quac-Job-Id": str(payload.get("job_id", "")),
        "X-Quac-Signature": _sign_payload(body),
    }

    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
        for attempt in range(WEBHOOK_MAX_ATTEMPTS):
            if attempt > 0:
                delay = _backoff_delay(attempt - 1)
                logger.info(
                    "webhook retry attempt=%d/%d delay=%.2fs url=%s",
                    attempt + 1,
                    WEBHOOK_MAX_ATTEMPTS,
                    delay,
                    url,
                )
                await asyncio.sleep(delay)
            try:
                response = await client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning(
                    "webhook attempt %d/%d raised %s: %s",
                    attempt + 1,
                    WEBHOOK_MAX_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                continue
            if response.status_code < 400:
                logger.info(
                    "webhook delivered status=%d attempt=%d job_id=%s",
                    response.status_code,
                    attempt + 1,
                    payload.get("job_id"),
                )
                return True
            if response.status_code < 500:
                logger.error(
                    "webhook rejected permanently status=%d url=%s body=%s",
                    response.status_code,
                    url,
                    response.text[:256],
                )
                return False
            logger.warning(
                "webhook attempt %d/%d status=%d",
                attempt + 1,
                WEBHOOK_MAX_ATTEMPTS,
                response.status_code,
            )

    logger.error(
        "webhook delivery exhausted after %d attempts url=%s", WEBHOOK_MAX_ATTEMPTS, url
    )
    return False


def dispatch_webhook(url: str, payload: dict[str, Any]) -> bool:
    """Synchronous bridge to the async dispatcher (invoked from the handler)."""
    try:
        return asyncio.run(_post_webhook_async(url, payload))
    except RuntimeError:
        # An event loop is already running in this thread; use a private loop.
        logger.warning("running webhook dispatch on a private event loop")
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_post_webhook_async(url, payload))
        finally:
            loop.close()
    except Exception:
        logger.exception("webhook dispatch crashed")
        return False


def _dispatch_cancelled_webhook(
    job_id: str, webhook_url: str | None, elapsed_ms: int
) -> None:
    """Notify the consumer that the job ended in ``cancelled`` state."""
    if not webhook_url:
        return
    dispatch_webhook(
        webhook_url,
        {
            "event": "simulation.cancelled",
            "job_id": job_id,
            "status": "cancelled",
            "error": "job cancelled",
            "error_type": "JobCancelledError",
            "simulation_time_ms": elapsed_ms,
            "completed_at": datetime.now(UTC).isoformat(),
            "download_url": None,
        },
    )


# --------------------------------------------------------------------------------------
# Failure handling — write diagnostics, mark failed, fire webhook, complete
# --------------------------------------------------------------------------------------


def _handle_business_failure(
    job_id: str,
    webhook_url: str | None,
    exc: BaseException,
    elapsed_ms: int,
) -> None:
    """Record a terminal failure without ever re-raising into SQS retry."""
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("job failed job_id=%s error=%s", job_id, exc)
    summary = f"{type(exc).__name__}: {exc}"
    trace_excerpt = trace[-ERROR_TRACE_MAX_CHARS:]
    _record_usage(job_id, elapsed_ms)

    try:
        _update_job_status(
            job_id,
            "failed",
            error_message=f"{summary}\n{trace_excerpt}",
            simulation_time_ms=elapsed_ms,
            unless_cancelled=True,
        )
    except JobCancelledError:
        # Cancel wins over the failure record: the consumer receives a
        # simulation.cancelled event instead of a failure event.
        logger.info("job cancelled while failing; failure record skipped job_id=%s", job_id)
        _dispatch_cancelled_webhook(job_id, webhook_url, elapsed_ms)
        return

    if webhook_url:
        dispatch_webhook(
            webhook_url,
            {
                "event": "simulation.failed",
                "job_id": job_id,
                "status": "failed",
                "error": summary,
                "error_type": type(exc).__name__,
                "simulation_time_ms": elapsed_ms,
                "completed_at": datetime.now(UTC).isoformat(),
                "download_url": None,
            },
        )


def _handle_timeout_failure(
    job_id: str,
    webhook_url: str | None,
    exc: SimulationTimeoutError,
    elapsed_ms: int,
) -> None:
    """Challenge #3 terminal path: dump ``Timeout Error`` and notify."""
    logger.error("timeout guard fired job_id=%s elapsed_ms=%d", job_id, elapsed_ms)
    _record_usage(job_id, elapsed_ms)
    try:
        _update_job_status(
            job_id,
            "failed",
            error_message=f"Timeout Error: {exc}",
            simulation_time_ms=elapsed_ms,
            unless_cancelled=True,
        )
    except JobCancelledError:
        logger.info("job cancelled while timing out; failure record skipped job_id=%s", job_id)
        _dispatch_cancelled_webhook(job_id, webhook_url, elapsed_ms)
        return

    if webhook_url:
        dispatch_webhook(
            webhook_url,
            {
                "event": "simulation.timeout",
                "job_id": job_id,
                "status": "failed",
                "error": f"Timeout Error: {exc}",
                "error_type": "SimulationTimeoutError",
                "simulation_time_ms": elapsed_ms,
                "completed_at": datetime.now(UTC).isoformat(),
                "download_url": None,
            },
        )


# --------------------------------------------------------------------------------------
# Record processing
# --------------------------------------------------------------------------------------


def _parse_message_body(raw_body: str) -> dict[str, Any]:
    """Parse and structurally verify the SQS envelope."""
    try:
        envelope = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"SQS body is not valid JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("SQS body must be a JSON object")
    job_id = envelope.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("SQS envelope missing 'job_id'")
    if "circuit_data" not in envelope:
        raise ValueError("SQS envelope missing 'circuit_data'")
    if not isinstance(envelope.get("format"), str):
        raise ValueError("SQS envelope missing 'format'")
    return envelope


def _safe_job_id(record: dict[str, Any]) -> str | None:
    """Best-effort extraction of ``job_id`` from a record, even if malformed."""
    try:
        envelope = _parse_message_body(str(record.get("body", "")))
        return str(envelope["job_id"])
    except (ValueError, KeyError, TypeError):
        return None


def _run_simulation_timed(envelope: dict[str, Any]) -> tuple[SimulationResult, int]:
    """Execute the simulator on the compute thread and measure its duration."""
    thread_started = time.perf_counter()
    result = run_simulation(
        envelope["circuit_data"],
        envelope["format"],
        int(envelope.get("shots", 1024)),
    )
    simulation_time_ms = int((time.perf_counter() - thread_started) * 1000)
    return result, simulation_time_ms


def _dispatch_success_webhook(
    webhook_url: str, job_id: str, simulation_result: SimulationResult,
    simulation_time_ms: int, completed_at: datetime, download_url: str,
) -> None:
    """Deliver the signed completion callback with the pre-signed URL."""
    top_counts = dict(
        sorted(simulation_result.counts.items(), key=lambda kv: -kv[1])[:8]
    )
    dispatch_webhook(
        webhook_url,
        {
            "event": "simulation.completed",
            "job_id": job_id,
            "status": "completed",
            "shots": simulation_result.shots,
            "method": simulation_result.method,
            "backend": simulation_result.backend,
            "selection_reasons": simulation_result.selection_reasons,
            "num_qubits": simulation_result.num_qubits,
            "depth": simulation_result.depth,
            "most_frequent": simulation_result.most_frequent,
            "top_counts": top_counts,
            "simulation_time_ms": simulation_time_ms,
            "completed_at": completed_at.isoformat(),
            "download_url": download_url,
            "download_expires_in_seconds": PRESIGNED_URL_TTL_SECONDS,
        },
    )


def _process_record(record: dict[str, Any], context: Any) -> None:
    """Full lifecycle for one SQS record: parse → simulate → store → notify.

    Raises:
            ValueError/KeyError/SimulationBuildError/SimulationExecutionError:
            business failures handled by the guarded wrapper.
            SimulationTimeoutError: fired by the SIGALRM guard.
            InfrastructureError: AWS-side failures requiring SQS redelivery.
    """
    message_id = str(record.get("messageId", "unknown"))

    try:
        envelope = _parse_message_body(str(record.get("body", "")))
    except ValueError as exc:
        # Unparseable envelopes are poison messages: mark the job failed (when
        # a job id can be recovered) and complete the record so it cannot
        # cycle through the queue forever.
        logger.error("poison message message_id=%s error=%s", message_id, exc)
        recovered = _safe_job_id(record) or "unknown"
        _update_job_status(recovered, "failed", error_message=f"malformed SQS envelope: {exc}")
        return

    job_id = str(envelope["job_id"])
    job_record = _read_job_record(job_id)
    stored_status = str(job_record.get("status", "")) if job_record else ""
    webhook_url = (
        str(envelope.get("webhook_url") or "")
        or str((job_record or {}).get("webhook_url") or "")
        or ""
    )
    started = time.perf_counter()
    budget = min(_remaining_budget_seconds(context), float(TIMEOUT_GUARD_SECONDS))

    # --- pre-execution guards -------------------------------------------------------
    # A cancelled job must never start computing, and a redelivered message for
    # an already-terminal job must not run (or bill) twice.
    if stored_status == "cancelled":
        logger.info("job already cancelled before execution job_id=%s", job_id)
        raise JobCancelledError(f"job {job_id} was cancelled before execution started")
    if stored_status in ("completed", "failed"):
        logger.info(
            "duplicate delivery for terminal job job_id=%s status=%s; acknowledging",
            job_id,
            stored_status,
        )
        return

    logger.info(
        "processing job_id=%s message_id=%s format=%s shots=%s budget=%.1fs",
        job_id,
        message_id,
        envelope.get("format"),
        envelope.get("shots"),
        budget,
    )

    _update_job_status(job_id, "processing", unless_cancelled=True)

    _arm_timeout_guard(int(budget))
    try:
        future = _get_executor().submit(_run_simulation_timed, envelope)
        try:
            simulation_result, simulation_time_ms = future.result(timeout=budget)
        except SimulationTimeoutError:
            elapsed = int((time.perf_counter() - started) * 1000)
            future.cancel()
            _handle_timeout_failure(
                job_id,
                webhook_url,
                SimulationTimeoutError(
                    f"simulation exceeded the {TIMEOUT_GUARD_SECONDS}s internal guard"
                ),
                elapsed,
            )
            return
        except TimeoutError as exc:
            # Secondary guard: future.result() timed out even though SIGALRM
            # did not fire (e.g. the compute thread swallowed the signal window).
            elapsed = int((time.perf_counter() - started) * 1000)
            future.cancel()
            _handle_timeout_failure(job_id, webhook_url, SimulationTimeoutError(str(exc)), elapsed)
            return
    except SimulationTimeoutError as exc:
        # Raised directly in the main thread while it waits on the future.
        elapsed = int((time.perf_counter() - started) * 1000)
        _handle_timeout_failure(job_id, webhook_url, exc, elapsed)
        return
    finally:
        _disarm_timeout_guard()

    # --- success path ---------------------------------------------------------------
    completed_at = datetime.now(UTC)
    key = _result_key(completed_at, job_id)
    document = {
        "job_id": job_id,
        "completed_at": completed_at.isoformat(),
        "shots": simulation_result.shots,
        "counts": simulation_result.counts,
        "most_frequent": simulation_result.most_frequent,
        "statevector_probabilities": simulation_result.statevector_probabilities,
        "method": simulation_result.method,
        "backend": simulation_result.backend,
        "selection_reasons": simulation_result.selection_reasons,
        "num_qubits": simulation_result.num_qubits,
        "depth": simulation_result.depth,
        "simulation_time_ms": simulation_time_ms,
        "metadata": simulation_result.metadata,
    }
    _upload_result(key, document)
    download_url = _presign_download(key)
    try:
        _update_job_status(
            job_id,
            "completed",
            simulation_time_ms=simulation_time_ms,
            result_s3_key=key,
            unless_cancelled=True,
        )
    except JobCancelledError:
        # Cancelled while finalising: the artifact must not outlive the job.
        _delete_result_object(key)
        _record_usage(job_id, simulation_time_ms)
        raise

    _record_usage(job_id, simulation_time_ms)
    logger.info("job completed job_id=%s time_ms=%d key=%s", job_id, simulation_time_ms, key)

    if webhook_url:
        _dispatch_success_webhook(
            webhook_url, job_id, simulation_result, simulation_time_ms, completed_at, download_url
        )


def _process_record_guarded(record: dict[str, Any], context: Any) -> bool:
    """Run one record, classifying failures as business (ok) or infra (retry).

    Returns:
            ``True`` when the record is safe to acknowledge, ``False`` when the
            batch should report it as a partial failure for AWS to redeliver.
    """
    message_id = str(record.get("messageId", "unknown"))
    started = time.perf_counter()

    try:
        _process_record(record, context)
        return True
    except JobCancelledError:
        _disarm_timeout_guard()
        job_id = _safe_job_id(record) or "unknown"
        webhook = _read_job_webhook_url(job_id)
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("job cancelled; work discarded job_id=%s elapsed_ms=%d", job_id, elapsed)
        _dispatch_cancelled_webhook(job_id, webhook, elapsed)
        return True
    except InfrastructureError:
        _disarm_timeout_guard()
        logger.exception("infrastructure failure; requesting redelivery message_id=%s", message_id)
        return False
    except (SimulationTimeoutError, TimeoutError) as exc:
        _disarm_timeout_guard()
        job_id = _safe_job_id(record) or "unknown"
        webhook = _read_job_webhook_url(job_id)
        elapsed = int((time.perf_counter() - started) * 1000)
        try:
            _handle_timeout_failure(
                job_id,
                webhook,
                (
                    exc
                    if isinstance(exc, SimulationTimeoutError)
                    else SimulationTimeoutError(str(exc))
                ),
                elapsed,
            )
            return True
        except InfrastructureError:
            logger.exception("timeout bookkeeping failed message_id=%s", message_id)
            return False
    except MemoryError as exc:
        _disarm_timeout_guard()
        job_id = _safe_job_id(record) or "unknown"
        webhook = _read_job_webhook_url(job_id)
        elapsed = int((time.perf_counter() - started) * 1000)
        try:
            _handle_business_failure(job_id, webhook, exc, elapsed)
            return True
        except InfrastructureError:
            logger.exception("OOM bookkeeping failed message_id=%s", message_id)
            return False
    except (SimulationBuildError, SimulationExecutionError, ValueError, KeyError) as exc:
        _disarm_timeout_guard()
        job_id = _safe_job_id(record) or "unknown"
        webhook = _read_job_webhook_url(job_id)
        elapsed = int((time.perf_counter() - started) * 1000)
        try:
            _handle_business_failure(job_id, webhook, exc, elapsed)
            return True
        except InfrastructureError:
            logger.exception("failure bookkeeping errored message_id=%s", message_id)
            return False
    except Exception as exc:
        _disarm_timeout_guard()
        job_id = _safe_job_id(record) or "unknown"
        webhook = _read_job_webhook_url(job_id)
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.exception("unexpected worker error message_id=%s", message_id)
        try:
            _handle_business_failure(job_id, webhook, exc, elapsed)
            return True
        except InfrastructureError:
            return False


# --------------------------------------------------------------------------------------
# Lambda entry point
# --------------------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda / SQS event-source entry point.

    Processes every record in the batch, reporting only *infrastructure*
    failures through ``batchItemFailures``. Business failures complete the
    record after writing diagnostics, so messages are never perpetually stuck
    in SQS retry cycles.
    """
    records = event.get("Records") or []
    logger.info("invocation started records=%d", len(records))

    batch_failures: list[dict[str, str]] = []
    for record in records:
        message_id = str(record.get("messageId", "unknown"))
        acknowledged = _process_record_guarded(record, context)
        if not acknowledged:
            batch_failures.append({"itemIdentifier": message_id})

    result = {"batchItemFailures": batch_failures}
    logger.info(
        "invocation finished processed=%d failed=%d", len(records), len(batch_failures)
    )
    return result


__all__ = [
    "InfrastructureError",
    "JobCancelledError",
    "SimulationTimeoutError",
    "dispatch_webhook",
    "lambda_handler",
]
