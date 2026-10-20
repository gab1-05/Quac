"""Asynchronous SQS FIFO publishing service for Quac.

Validated simulation payloads are handed to this module for durable, ordered
enqueueing onto an AWS SQS FIFO queue. The module derives the
``MessageDeduplicationId`` deterministically so that re-submissions of the
same circuit are collapsed at the broker:

* If the consumer supplied an ``idempotency_key`` it takes precedence and is
  hashed with SHA-256.
* Otherwise a SHA-256 digest of the canonicalised circuit payload
  (``circuit_data`` + ``format`` + ``shots``) is used.

Both branches are bounded to SQS' 128-character dedup-ID ceiling.

Blocking ``boto3`` calls are executed on the event loop's default thread
pooler via :func:`asyncio.to_thread`, giving the FastAPI process a true async
publish path while reusing a single pooled SQS client.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from typing import Any, Final

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from .schemas import SimulationRequest

logger = logging.getLogger("quac.api.queue")

DEFAULT_QUEUE_URL: Final[str] = "https://sqs.us-east-1.amazonaws.com/000000000000/quac-jobs.fifo"
DEFAULT_REGION: Final[str] = "us-east-1"
DEDUP_ID_MAX_LENGTH: Final[int] = 128
MESSAGE_GROUP_TTL: Final[int] = 300

_BOTO_CONFIG: Final[BotoConfig] = BotoConfig(
    max_pool_connections=16,
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=3,
    read_timeout=15,
    user_agent_extra="quac-api/1.0.0",
)


class QueuePublishError(RuntimeError):
    """Raised when a message could not be enqueued onto the FIFO queue."""


def canonical_payload_fingerprint(request: SimulationRequest) -> str:
    """Return a stable SHA-256 fingerprint of the semantic request content.

    The canonical document orders keys and uses compact separators so that
    logically identical submissions always produce the same digest regardless
    of client JSON key ordering or whitespace.
    """
    canonical = json.dumps(
        {
            "circuit_data": request.circuit_data,
            "format": request.format.value,
            "shots": request.shots,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_deduplication_id(request: SimulationRequest) -> str:
    """Derive the SQS ``MessageDeduplicationId`` for a request.

    Precedence: explicit ``idempotency_key`` > payload fingerprint. The result
    is always a 64-character lowercase hex string, well inside the 128-char
    broker ceiling regardless of how the client key was shaped.
    """
    if request.idempotency_key:
        raw = f"quac:idem:{request.idempotency_key}"
    else:
        raw = f"quac:payload:{canonical_payload_fingerprint(request)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if len(digest) > DEDUP_ID_MAX_LENGTH:  # pragma: no cover - hex digest is always 64 chars
        digest = hashlib.sha256(digest.encode("utf-8")).hexdigest()
    return digest


def build_message_body(job_id: str, request: SimulationRequest, qubit_count: int) -> str:
    """Serialise the queue envelope consumed by the Quac worker."""
    return json.dumps(
        {
            "job_id": job_id,
            "submitted_at": request.model_dump(
                mode="json", include={"idempotency_key", "shots", "format"}
            ),
            "circuit_data": request.circuit_data,
            "format": request.format.value,
            "shots": request.shots,
            "webhook_url": str(request.webhook_url),
            "idempotency_key": request.idempotency_key,
            "qubit_count": qubit_count,
            "schema_version": 1,
        },
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


class FifoPublisher:
    """Pooled, lifecycle-managed publisher for the Quac FIFO queue."""

    def __init__(self, queue_url: str, region: str) -> None:
        self._queue_url = queue_url
        self._region = region
        self._client: Any | None = None
        self._lock = threading.Lock()

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                logger.debug("initialising SQS client region=%s", self._region)
                self._client = boto3.client("sqs", region_name=self._region, config=_BOTO_CONFIG)
        return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    logger.debug("ignored error while closing SQS client", exc_info=True)
            self._client = None

    def publish(
        self,
        job_id: str,
        request: SimulationRequest,
        qubit_count: int,
    ) -> str:
        """Blocking send; returns the broker ``MessageId``."""
        client = self._ensure_client()
        dedup_id = derive_deduplication_id(request)
        body = build_message_body(job_id, request, qubit_count)
        try:
            response = client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
                MessageGroupId=job_id,
                MessageDeduplicationId=dedup_id,
                MessageAttributes={
                    "format": {"DataType": "String", "StringValue": request.format.value},
                    "shots": {"DataType": "Number", "StringValue": str(request.shots)},
                    "qubit_count": {"DataType": "Number", "StringValue": str(qubit_count)},
                },
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("SQS send_message failed job_id=%s error=%s", job_id, exc)
            raise QueuePublishError(f"failed to enqueue job {job_id}: {exc}") from exc

        message_id = str(response.get("MessageId", ""))
        logger.info(
            "enqueued job job_id=%s message_id=%s dedup_id=%s qubits=%d",
            job_id,
            message_id,
            dedup_id[:16],
            qubit_count,
        )
        return message_id


# --------------------------------------------------------------------------------------
# Process-wide publisher
# --------------------------------------------------------------------------------------

_publisher: FifoPublisher | None = None
_publisher_lock = threading.Lock()


def _get_publisher() -> FifoPublisher:
    global _publisher
    if _publisher is None:
        with _publisher_lock:
            if _publisher is None:
                _publisher = FifoPublisher(
                    queue_url=os.getenv("QUAC_QUEUE_URL", DEFAULT_QUEUE_URL),
                    region=os.getenv("QUAC_AWS_REGION", os.getenv("AWS_REGION", DEFAULT_REGION)),
                )
    return _publisher


def shutdown() -> None:
    """Release the pooled SQS client during application teardown."""
    global _publisher
    with _publisher_lock:
        if _publisher is not None:
            _publisher.close()
            _publisher = None


async def enqueue_simulation(
    job_id: str,
    request: SimulationRequest,
    qubit_count: int,
) -> str:
    """Publish a validated simulation request onto the FIFO queue.

    Args:
            job_id: UUIDv4 identifier already persisted in DynamoDB.
            request: The validated ingestion contract.
            qubit_count: Width confirmed by the validation firewall.

    Returns:
            The SQS broker ``MessageId``.

    Raises:
            QueuePublishError: when the broker rejects the send.
    """
    publisher = _get_publisher()
    return await asyncio.to_thread(publisher.publish, job_id, request, qubit_count)


__all__ = [
    "FifoPublisher",
    "QueuePublishError",
    "build_message_body",
    "canonical_payload_fingerprint",
    "derive_deduplication_id",
    "enqueue_simulation",
    "shutdown",
]
