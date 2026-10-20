"""Private result-artifact access for the Quac API.

The worker persists full simulation result documents in S3 under
``results/{Y}/{M}/{D}/{job_id}.json``. This module gives the API two bounded
operations against those objects:

* :func:`fetch_result_document` — read and parse a result JSON (with a hard
  inline-size ceiling so a pathological object cannot balloon API memory),
* :func:`presign_result_url` — issue a short-lived pre-signed GET URL so
  authorized consumers can download the full document directly from S3.

Blocking ``boto3`` calls run off the event loop via :func:`asyncio.to_thread`,
matching the pattern used by :mod:`api.database` and :mod:`api.queue_service`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Any, Final

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger("quac.api.results")

DEFAULT_RESULTS_BUCKET: Final[str] = "quac-results"
DEFAULT_REGION: Final[str] = "us-east-1"
PRESIGNED_URL_TTL_SECONDS: Final[int] = int(os.getenv("QUAC_PRESIGNED_TTL", "900"))
MAX_INLINE_DOCUMENT_BYTES: Final[int] = 512 * 1024
"""Result documents larger than this are not inlined into API responses.

Counts and metadata always fit comfortably; the usual offender is a dense
statevector probability map for a large register. The pre-signed URL always
offers the complete document.
"""

_BOTO_CONFIG: Final[BotoConfig] = BotoConfig(
    max_pool_connections=16,
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=3,
    read_timeout=15,
    user_agent_extra="quac-api/1.0.0",
)


class ResultNotFound(Exception):
    """The referenced result object does not exist in the results bucket."""


class ResultUnavailable(Exception):
    """S3 could not be reached or returned an unreadable payload."""


class ResultStore:
    """Lifecycle-managed S3 handle scoped to the results bucket."""

    def __init__(self, bucket_name: str, region: str) -> None:
        self._bucket_name = bucket_name
        self._region = region
        self._client: Any | None = None
        self._lock = threading.Lock()

    def _ensure_client(self) -> Any:
        """Create the pooled S3 client exactly once per process."""
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                logger.debug(
                    "initialising S3 client bucket=%s region=%s",
                    self._bucket_name,
                    self._region,
                )
                self._client = boto3.client("s3", region_name=self._region, config=_BOTO_CONFIG)
        return self._client

    def close(self) -> None:
        """Release the pooled client (safe to call repeatedly)."""
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    logger.debug("ignored error while closing S3 client", exc_info=True)
            self._client = None

    # -- synchronous operations ----------------------------------------------------------

    def fetch_document(self, key: str) -> dict[str, Any]:
        """Read and parse a result document.

        Raises:
            ResultNotFound: object is absent (404 / NoSuchKey).
            ResultUnavailable: transport failure or non-JSON payload.
        """
        client = self._ensure_client()
        try:
            response = client.get_object(Bucket=self._bucket_name, Key=key)
            body = response["Body"].read(MAX_INLINE_DOCUMENT_BYTES + 1)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                raise ResultNotFound(key) from exc
            logger.error("s3 get failed bucket=%s key=%s error=%s", self._bucket_name, key, exc)
            raise ResultUnavailable(f"result object unreadable: {exc}") from exc
        except (BotoCoreError, OSError) as exc:
            logger.error("s3 get failed bucket=%s key=%s error=%s", self._bucket_name, key, exc)
            raise ResultUnavailable(f"result store unreachable: {exc}") from exc

        if len(body) > MAX_INLINE_DOCUMENT_BYTES:
            raise ResultUnavailable("result document exceeds the inline size ceiling")

        try:
            document = json.loads(body)
        except json.JSONDecodeError as exc:
            logger.error("result document is not JSON key=%s", key)
            raise ResultUnavailable("result document is not valid JSON") from exc
        if not isinstance(document, dict):
            raise ResultUnavailable("result document is not a JSON object")
        return document

    def presign(self, key: str) -> str:
        """Issue a time-boxed pre-signed GET URL for a result object."""
        client = self._ensure_client()
        try:
            return str(
                client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket_name, "Key": key},
                    ExpiresIn=PRESIGNED_URL_TTL_SECONDS,
                )
            )
        except (ClientError, BotoCoreError) as exc:
            logger.error("presign failed key=%s error=%s", key, exc)
            raise ResultUnavailable(f"could not sign download URL: {exc}") from exc


# --------------------------------------------------------------------------------------
# Process-wide store instance
# --------------------------------------------------------------------------------------

_store: ResultStore | None = None
_store_lock = threading.Lock()


def _get_store() -> ResultStore:
    """Return the process-wide store, creating it on first use."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ResultStore(
                    bucket_name=os.getenv("QUAC_RESULTS_BUCKET", DEFAULT_RESULTS_BUCKET),
                    region=os.getenv("QUAC_AWS_REGION", os.getenv("AWS_REGION", DEFAULT_REGION)),
                )
    return _store


def shutdown() -> None:
    """Release the pooled S3 client during application teardown."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None


# --------------------------------------------------------------------------------------
# Async public API
# --------------------------------------------------------------------------------------


async def fetch_result_document(key: str) -> dict[str, Any]:
    """Fetch a result document off the event loop."""
    store = _get_store()
    return await asyncio.to_thread(store.fetch_document, key)


async def presign_result_url(key: str) -> str:
    """Presign a result download URL off the event loop."""
    store = _get_store()
    return await asyncio.to_thread(store.presign, key)


__all__ = [
    "MAX_INLINE_DOCUMENT_BYTES",
    "PRESIGNED_URL_TTL_SECONDS",
    "ResultNotFound",
    "ResultStore",
    "ResultUnavailable",
    "fetch_result_document",
    "presign_result_url",
    "shutdown",
]
