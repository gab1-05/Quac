"""DynamoDB persistence adapter for Quac job records.

Provides a thread-safe, lazily-initialised connection lifecycle wrapper over
``boto3``'s DynamoDB resource. The resource (and its underlying HTTP connection
pool) is created exactly once per process and reused for every subsequent
operation, which keeps the FastAPI event loop free of repeated credential and
endpoint resolution.

Public async surface:

* :func:`create_job_record` — persist a new ``queued`` job.
* :func:`update_job_status` — transition a job, optionally recording an error.
* :func:`get_job_record` — fetch a job by ``job_id``.
* :func:`cancel_job_record` — atomically cancel a non-terminal job.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import UTC, datetime
from typing import Any, Final

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .schemas import CircuitFormat, JobStatus

logger = logging.getLogger("quac.api.database")

DEFAULT_TABLE_NAME: Final[str] = "quac-jobs"
DEFAULT_REGION: Final[str] = "us-east-1"
CONNECTION_POOL_SIZE: Final[int] = 32

DEFAULT_OWNER_ID: Final[str] = "system"
"""Owner stamped on records created before (or without) authenticated identity."""

RECORD_SCHEMA_VERSION: Final[int] = 1
"""Version of the job-record attribute schema; bumped on breaking shape changes."""

_BOTO_CONFIG: Final[BotoConfig] = BotoConfig(
    max_pool_connections=CONNECTION_POOL_SIZE,
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=3,
    read_timeout=10,
    user_agent_extra="quac-api/1.0.0",
)


class JobRecordNotFoundError(LookupError):
    """Raised when no DynamoDB item exists for the requested ``job_id``."""


class JobNotCancellableError(LookupError):
    """Raised when a job exists but has already reached a terminal state."""

    def __init__(self, job_id: str, current_status: str) -> None:
        self.job_id = job_id
        self.current_status = current_status
        super().__init__(f"job {job_id} is already {current_status}")


class DynamoJobStore:
    """Lifecycle-managed DynamoDB handle for the ``quac-jobs`` table."""

    def __init__(self, table_name: str, region: str) -> None:
        self._table_name = table_name
        self._region = region
        self._resource: Any | None = None
        self._table: Any | None = None
        self._lock = threading.Lock()

    # -- connection lifecycle ----------------------------------------------------------

    def _ensure_resource(self) -> Any:
        """Create the pooled DynamoDB resource exactly once per process."""
        if self._table is not None:
            return self._table
        with self._lock:
            if self._table is None:
                logger.debug(
                    "initialising DynamoDB resource table=%s region=%s pool=%d",
                    self._table_name,
                    self._region,
                    CONNECTION_POOL_SIZE,
                )
                self._resource = boto3.resource(
                    "dynamodb", region_name=self._region, config=_BOTO_CONFIG
                )
                self._table = self._resource.Table(self._table_name)
        assert self._table is not None
        return self._table

    def close(self) -> None:
        """Release the underlying resource (safe to call multiple times)."""
        with self._lock:
            if self._resource is not None:
                session = getattr(self._resource, "meta", None)
                endpoint = getattr(session, "client", None)
                if callable(endpoint):
                    try:
                        client = endpoint()
                        client.close()
                    except Exception:
                        logger.debug("ignored error while closing DynamoDB client", exc_info=True)
            self._resource = None
            self._table = None

    # -- synchronous operations (invoked through asyncio.to_thread) ---------------------

    def create_job(
        self,
        job_id: str,
        payload_json: str,
        *,
        circuit_format: CircuitFormat,
        shots: int,
        webhook_url: str | None,
        qubit_count: int,
        idempotency_key: str | None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Insert a new job item in ``queued`` state."""
        table = self._ensure_resource()
        now = datetime.now(UTC).isoformat()
        item: dict[str, Any] = {
            "job_id": job_id,
            "status": JobStatus.QUEUED.value,
            "created_at": now,
            "completed_at": None,
            "error_message": None,
            "payload": payload_json,
            "format": circuit_format.value,
            "shots": shots,
            "webhook_url": webhook_url,
            "qubit_count": qubit_count,
            "idempotency_key": idempotency_key,
            "simulation_time_ms": None,
            "result_s3_key": None,
            # Multi-tenancy groundwork: every record knows its owner so tenant
            # isolation is enforced in queries (GSI owner_id-index) rather than
            # in application filtering alone.
            "owner_id": owner_id or DEFAULT_OWNER_ID,
            "schema_version": RECORD_SCHEMA_VERSION,
        }
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(job_id)",
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ConditionalCheckFailedException":
                raise FileExistsError(f"job {job_id} already exists") from exc
            logger.error("DynamoDB put_item failed job_id=%s code=%s", job_id, error_code)
            raise
        logger.info("job record created job_id=%s qubits=%d shots=%d", job_id, qubit_count, shots)
        return item

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        error_message: str | None = None,
        simulation_time_ms: int | None = None,
        result_s3_key: str | None = None,
    ) -> dict[str, Any]:
        """Transition a job and store any terminal diagnostics."""
        table = self._ensure_resource()
        terminal = status in (JobStatus.COMPLETED, JobStatus.FAILED)
        expression_names: dict[str, str] = {"#status": "status"}
        expression_values: dict[str, Any] = {":status": status.value}

        updates: list[str] = ["#status = :status"]
        if error_message is not None:
            updates.append("error_message = :err")
            expression_values[":err"] = error_message[:4000]
        if simulation_time_ms is not None:
            updates.append("simulation_time_ms = :ms")
            expression_values[":ms"] = simulation_time_ms
        if result_s3_key is not None:
            updates.append("result_s3_key = :key")
            expression_values[":key"] = result_s3_key
        if terminal:
            updates.append("completed_at = :done")
            expression_values[":done"] = datetime.now(UTC).isoformat()

        try:
            response = table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET " + ", ".join(updates),
                ExpressionAttributeNames=expression_names,
                ExpressionAttributeValues=expression_values,
                ConditionExpression="attribute_exists(job_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "ConditionalCheckFailedException":
                raise JobRecordNotFoundError(job_id) from exc
            logger.error("DynamoDB update_item failed job_id=%s code=%s", job_id, error_code)
            raise

        attributes = response.get("Attributes", {})
        logger.info("job record updated job_id=%s status=%s", job_id, status.value)
        return dict(attributes)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Atomically move a non-terminal job to ``cancelled``.

        The transition is guarded by a condition expression so concurrent
        completions (worker finishing while the API cancels) resolve to exactly
        one winner instead of silently overwriting a terminal state.

        Raises:
            JobRecordNotFoundError: when no item exists for ``job_id``.
            JobNotCancellableError: when the job already reached a terminal
                state (completed / failed / cancelled).
        """
        table = self._ensure_resource()
        try:
            response = table.update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #status = :cancelled, completed_at = :done",
                ConditionExpression=(
                    "attribute_exists(job_id) AND (#status = :queued OR #status = :processing)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":cancelled": JobStatus.CANCELLED.value,
                    ":queued": JobStatus.QUEUED.value,
                    ":processing": JobStatus.PROCESSING.value,
                    ":done": datetime.now(UTC).isoformat(),
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "Unknown")
            if error_code != "ConditionalCheckFailedException":
                logger.error("DynamoDB cancel failed job_id=%s code=%s", job_id, error_code)
                raise
            # The condition failed: either the record is missing (404) or the
            # job is already terminal (409). One extra read disambiguates.
            current = self.get(job_id)  # raises JobRecordNotFoundError when absent
            raise JobNotCancellableError(job_id, str(current.get("status", "unknown"))) from exc

        attributes = response.get("Attributes", {})
        logger.info("job cancelled job_id=%s", job_id)
        return dict(attributes)

    def get(self, job_id: str) -> dict[str, Any]:
        """Fetch a job item or raise :class:`JobRecordNotFoundError`."""
        table = self._ensure_resource()
        try:
            response = table.get_item(Key={"job_id": job_id})
        except ClientError:
            logger.error("DynamoDB get_item failed job_id=%s", job_id)
            raise
        item = response.get("Item")
        if item is None:
            raise JobRecordNotFoundError(job_id)
        return dict(item)


# --------------------------------------------------------------------------------------
# Process-wide store instance
# --------------------------------------------------------------------------------------

_store: DynamoJobStore | None = None
_store_lock = threading.Lock()


def _get_store() -> DynamoJobStore:
    """Return the process-wide store, creating it on first use."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DynamoJobStore(
                    table_name=os.getenv("QUAC_JOBS_TABLE", DEFAULT_TABLE_NAME),
                    region=os.getenv("QUAC_AWS_REGION", os.getenv("AWS_REGION", DEFAULT_REGION)),
                )
    return _store


def initialise(table_name: str | None = None, region: str | None = None) -> DynamoJobStore:
    """Explicitly (re)bind the process-wide store, e.g. during app lifespan."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        resolved_table = table_name
        if resolved_table is None:
            resolved_table = os.getenv("QUAC_JOBS_TABLE", DEFAULT_TABLE_NAME)
        resolved_region = region
        if resolved_region is None:
            resolved_region = os.getenv("QUAC_AWS_REGION", os.getenv("AWS_REGION", DEFAULT_REGION))
        _store = DynamoJobStore(table_name=resolved_table, region=resolved_region)
        return _store


def shutdown() -> None:
    """Close the process-wide store during application teardown."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None


# --------------------------------------------------------------------------------------
# Async public API
# --------------------------------------------------------------------------------------


async def create_job_record(
    job_id: str,
    payload_json: str,
    *,
    circuit_format: CircuitFormat,
    shots: int,
    webhook_url: str | None,
    qubit_count: int,
    idempotency_key: str | None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Persist a new job record off the event loop."""
    store = _get_store()
    return await asyncio.to_thread(
        store.create_job,
        job_id,
        payload_json,
        circuit_format=circuit_format,
        shots=shots,
        webhook_url=webhook_url,
        qubit_count=qubit_count,
        idempotency_key=idempotency_key,
        owner_id=owner_id,
    )


async def update_job_status(
    job_id: str,
    status: JobStatus,
    *,
    error_message: str | None = None,
    simulation_time_ms: int | None = None,
    result_s3_key: str | None = None,
) -> dict[str, Any]:
    """Transition a job record off the event loop."""
    store = _get_store()
    return await asyncio.to_thread(
        store.update_status,
        job_id,
        status,
        error_message=error_message,
        simulation_time_ms=simulation_time_ms,
        result_s3_key=result_s3_key,
    )


async def get_job_record(job_id: str) -> dict[str, Any]:
    """Fetch a job record off the event loop."""
    store = _get_store()
    return await asyncio.to_thread(store.get, job_id)


async def cancel_job_record(job_id: str) -> dict[str, Any]:
    """Cancel a non-terminal job off the event loop.

    Raises:
        JobRecordNotFoundError: unknown ``job_id``.
        JobNotCancellableError: job already reached a terminal state.
    """
    store = _get_store()
    return await asyncio.to_thread(store.cancel, job_id)


__all__ = [
    "DEFAULT_OWNER_ID",
    "RECORD_SCHEMA_VERSION",
    "CircuitFormat",
    "DynamoJobStore",
    "JobNotCancellableError",
    "JobRecordNotFoundError",
    "cancel_job_record",
    "create_job_record",
    "get_job_record",
    "initialise",
    "shutdown",
    "update_job_status",
]
