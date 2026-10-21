"""Identity entities in the ``quac-jobs`` single table (P1).

Item layout (all share the table's ``job_id`` hash key):

* ``USER#<username>`` — user_id, username, password_hash (argon2id), plan,
  created_at, ``entity="user"``, owner_id (= own user_id for GSI uniformity).
* ``KEY#<key_id>`` — API keys. secret_hash is HMAC-SHA256(secret, pepper);
  the secret itself is shown to the user exactly once and never stored.
  ``owner_username`` is denormalised so key auth can resolve the user row
  with one extra read (the GSI only indexes user_id, not username).
* ``SESSION#<sha256(token)>`` — sessions. The raw cookie value is never
  stored; ``expires_at`` (epoch seconds) doubles as the DynamoDB TTL
  attribute *and* is checked in code (TTL deletion is lazy, up to 48h).
* ``USAGE#<user_id>#<YYYY-MM>`` — monthly counters (requests, compute_ms)
  updated with atomic conditional ADD expressions.
* ``AUDIT#<user_id>#<iso-ts>#<rand>`` — append-only audit trail with a ~400
  day TTL.

Every identity item carries ``owner_id`` and ``created_at`` so it is
projected through the ``owner_id-index`` GSI uniformly; job queries filter on
``entity`` to keep these rows out of customer job listings.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

from . import database

logger = logging.getLogger("quac.api.identity")

USER_PREFIX = "USER#"
KEY_PREFIX = "KEY#"
SESSION_PREFIX = "SESSION#"
USAGE_PREFIX = "USAGE#"
AUDIT_PREFIX = "AUDIT#"

AUDIT_TTL_SECONDS = 400 * 24 * 3600  # ~400 days, matches the table TTL attr
USAGE_MONTH_FORMAT = "%Y-%m"


class UsernameTakenError(LookupError):
    """Raised when registration collides with an existing username."""


class QuotaExceededError(LookupError):
    """Raised when the conditional monthly-request increment is refused."""

    def __init__(self, month: str, limit: int) -> None:
        self.month = month
        self.limit = limit
        super().__init__(f"monthly request quota ({limit}) exhausted for {month}")


# --------------------------------------------------------------------------------------
# Item builders
# --------------------------------------------------------------------------------------


def user_key(username: str) -> str:
    """Table key for a user row."""
    return f"{USER_PREFIX}{username}"


def key_key(key_id: str) -> str:
    """Table key for an API-key row."""
    return f"{KEY_PREFIX}{key_id}"


def session_key(token_hash: str) -> str:
    """Table key for a session row (hash of the raw cookie value)."""
    return f"{SESSION_PREFIX}{token_hash}"


def usage_key(user_id: str, month: str) -> str:
    """Table key for a monthly usage counter row."""
    return f"{USAGE_PREFIX}{user_id}#{month}"


def audit_key(user_id: str, now: datetime) -> str:
    """Table key for an audit row (timestamp + random suffix for uniqueness)."""
    return f"{AUDIT_PREFIX}{user_id}#{now.isoformat()}#{uuid.uuid4().hex[:8]}"


def current_month(now: datetime | None = None) -> str:
    """UTC calendar month in ``YYYY-MM`` form."""
    return (now or datetime.now(UTC)).strftime(USAGE_MONTH_FORMAT)


# --------------------------------------------------------------------------------------
# Synchronous internals (invoked through asyncio.to_thread)
# --------------------------------------------------------------------------------------


def _create_user_sync(username: str, password_hash: str, plan: str) -> dict[str, Any]:
    """Conditional-put a user row; raise :class:`UsernameTakenError` on collision."""
    table = database.get_table()
    now = datetime.now(UTC)
    user_id = str(uuid.uuid4())
    item: dict[str, Any] = {
        "job_id": user_key(username),
        "entity": "user",
        "user_id": user_id,
        "username": username,
        "password_hash": password_hash,
        "plan": plan,
        "created_at": now.isoformat(),
        "owner_id": user_id,
    }
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ConditionalCheckFailedException":
            raise UsernameTakenError(username) from exc
        logger.error("user put failed username=%s code=%s", username, error_code)
        raise
    logger.info("user registered user_id=%s plan=%s", user_id, plan)
    return item


def _get_user_sync(username: str) -> dict[str, Any] | None:
    """Fetch a user row by username, or ``None``."""
    table = database.get_table()
    response = table.get_item(Key={"job_id": user_key(username)})
    return dict(response["Item"]) if "Item" in response else None


def _create_session_sync(
    token_hash: str, user_id: str, username: str, ttl_seconds: int
) -> dict[str, Any]:
    """Store a session row with an epoch-seconds expiry (TTL attribute)."""
    table = database.get_table()
    now = datetime.now(UTC)
    item: dict[str, Any] = {
        "job_id": session_key(token_hash),
        "entity": "session",
        "user_id": user_id,
        "username": username,
        "owner_id": user_id,
        "created_at": now.isoformat(),
        "expires_at": int(now.timestamp()) + ttl_seconds,
    }
    table.put_item(Item=item)
    return item


def _get_session_sync(token_hash: str) -> dict[str, Any] | None:
    """Fetch a session row; treat past-expiry rows as absent (lazy TTL)."""
    table = database.get_table()
    response = table.get_item(Key={"job_id": session_key(token_hash)})
    if "Item" not in response:
        return None
    item = dict(response["Item"])
    expires_at = int(item.get("expires_at", 0))
    if expires_at <= int(time.time()):
        # Best-effort delete of the expired row; the read already failed closed.
        try:
            table.delete_item(Key={"job_id": session_key(token_hash)})
        except ClientError:
            logger.debug("expired session cleanup failed", exc_info=True)
        return None
    return item


def _delete_session_sync(token_hash: str) -> None:
    """Delete a session row (logout)."""
    table = database.get_table()
    table.delete_item(Key={"job_id": session_key(token_hash)})


def _create_api_key_sync(
    key_id: str,
    user_id: str,
    username: str,
    secret_hash: str,
    prefix: str,
    label: str,
) -> dict[str, Any]:
    """Store an API-key row (secret already hashed by the caller)."""
    table = database.get_table()
    now = datetime.now(UTC)
    item: dict[str, Any] = {
        "job_id": key_key(key_id),
        "entity": "api_key",
        "key_id": key_id,
        "secret_hash": secret_hash,
        "prefix": prefix,
        "label": label,
        "owner_id": user_id,
        "owner_username": username,
        "created_at": now.isoformat(),
        "revoked": False,
    }
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
    logger.info("api key created key_id=%s owner=%s", key_id, username)
    return item


def _get_api_key_sync(key_id: str) -> dict[str, Any] | None:
    """Fetch an API-key row by id, or ``None``."""
    table = database.get_table()
    response = table.get_item(Key={"job_id": key_key(key_id)})
    return dict(response["Item"]) if "Item" in response else None


def _list_api_keys_sync(user_id: str) -> list[dict[str, Any]]:
    """List a user's API keys via the owner GSI, newest first."""
    from boto3.dynamodb.conditions import Key as DdbKey

    table = database.get_table()
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "IndexName": "owner_id-index",
        "KeyConditionExpression": DdbKey("owner_id").eq(user_id),
        "FilterExpression": "begins_with(job_id, :prefix) AND #e = :entity",
        "ExpressionAttributeNames": {"#e": "entity"},
        "ExpressionAttributeValues": {":prefix": KEY_PREFIX, ":entity": "api_key"},
        "ScanIndexForward": False,
    }
    while True:
        response = table.query(**kwargs)
        items.extend(dict(item) for item in response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _revoke_api_key_sync(user_id: str, key_id: str) -> bool:
    """Revoke a key the caller owns; ``False`` when missing or foreign."""
    table = database.get_table()
    try:
        table.update_item(
            Key={"job_id": key_key(key_id)},
            UpdateExpression="SET revoked = :true",
            ConditionExpression="attribute_exists(job_id) AND owner_id = :user",
            ExpressionAttributeValues={":true": True, ":user": user_id},
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ConditionalCheckFailedException":
            return False
        logger.error("api key revoke failed key_id=%s code=%s", key_id, error_code)
        raise
    logger.info("api key revoked key_id=%s owner=%s", key_id, user_id)
    return True


def _get_usage_sync(user_id: str, month: str) -> dict[str, Any] | None:
    """Fetch the monthly usage counters, or ``None`` before first use."""
    table = database.get_table()
    response = table.get_item(Key={"job_id": usage_key(user_id, month)})
    return dict(response["Item"]) if "Item" in response else None


def _reserve_request_quota_sync(user_id: str, month: str, max_requests: int) -> int:
    """Atomically increment the monthly request counter, capped at the plan.

    Conditional ADD: the increment only lands when the current value is below
    the cap, so concurrent submits cannot over-admit past the plan limit.
    Raises :class:`QuotaExceededError` when the cap is already reached.
    """
    table = database.get_table()
    try:
        response = table.update_item(
            Key={"job_id": usage_key(user_id, month)},
            UpdateExpression=(
                "ADD requests :one "
                "SET owner_id = if_not_exists(owner_id, :owner), "
                "created_at = if_not_exists(created_at, :created), "
                "#e = if_not_exists(#e, :entity), "
                "#month = if_not_exists(#month, :month_value)"
            ),
            ConditionExpression="attribute_not_exists(requests) OR requests < :max",
            ExpressionAttributeNames={"#e": "entity", "#month": "month"},
            ExpressionAttributeValues={
                ":one": 1,
                ":max": max_requests,
                ":owner": user_id,
                ":created": datetime.now(UTC).isoformat(),
                ":entity": "usage",
                ":month_value": month,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "ConditionalCheckFailedException":
            raise QuotaExceededError(month, max_requests) from exc
        logger.error("usage increment failed user=%s code=%s", user_id, error_code)
        raise
    attributes = response.get("Attributes", {})
    return int(attributes.get("requests", 0))


def _record_audit_sync(user_id: str, event: str, detail: dict[str, Any]) -> None:
    """Append one audit row (fire-and-forget from the request path)."""
    table = database.get_table()
    now = datetime.now(UTC)
    item: dict[str, Any] = {
        "job_id": audit_key(user_id, now),
        "entity": "audit",
        "event": event,
        "detail": detail,
        "owner_id": user_id,
        "created_at": now.isoformat(),
        "expires_at": int(now.timestamp()) + AUDIT_TTL_SECONDS,
    }
    table.put_item(Item=item)


# --------------------------------------------------------------------------------------
# Async public API
# --------------------------------------------------------------------------------------


async def create_user(username: str, password_hash: str, plan: str) -> dict[str, Any]:
    """Register a new user; raises :class:`UsernameTakenError` on collision."""
    return await asyncio.to_thread(_create_user_sync, username, password_hash, plan)


async def get_user(username: str) -> dict[str, Any] | None:
    """Fetch a user row by username."""
    return await asyncio.to_thread(_get_user_sync, username)


async def create_session(
    token_hash: str, user_id: str, username: str, ttl_seconds: int
) -> dict[str, Any]:
    """Persist a session row for a freshly-minted cookie token."""
    return await asyncio.to_thread(
        _create_session_sync, token_hash, user_id, username, ttl_seconds
    )


async def get_session(token_hash: str) -> dict[str, Any] | None:
    """Fetch a live session row; expired rows read as ``None``."""
    return await asyncio.to_thread(_get_session_sync, token_hash)


async def delete_session(token_hash: str) -> None:
    """Delete a session row (logout)."""
    await asyncio.to_thread(_delete_session_sync, token_hash)


async def create_api_key(
    key_id: str,
    user_id: str,
    username: str,
    secret_hash: str,
    prefix: str,
    label: str,
) -> dict[str, Any]:
    """Persist a new API-key row."""
    return await asyncio.to_thread(
        _create_api_key_sync, key_id, user_id, username, secret_hash, prefix, label
    )


async def get_api_key(key_id: str) -> dict[str, Any] | None:
    """Fetch an API-key row by id."""
    return await asyncio.to_thread(_get_api_key_sync, key_id)


async def list_api_keys(user_id: str) -> list[dict[str, Any]]:
    """List all API keys belonging to a user, newest first."""
    return await asyncio.to_thread(_list_api_keys_sync, user_id)


async def revoke_api_key(user_id: str, key_id: str) -> bool:
    """Revoke a key; ``False`` when it does not exist or is not the caller's."""
    return await asyncio.to_thread(_revoke_api_key_sync, user_id, key_id)


async def get_usage(user_id: str, month: str) -> dict[str, Any] | None:
    """Fetch the monthly usage counters for a user."""
    return await asyncio.to_thread(_get_usage_sync, user_id, month)


async def reserve_request_quota(user_id: str, month: str, max_requests: int) -> int:
    """Increment the monthly request counter; raises :class:`QuotaExceededError`."""
    return await asyncio.to_thread(_reserve_request_quota_sync, user_id, month, max_requests)


async def record_audit(user_id: str, event: str, detail: dict[str, Any]) -> None:
    """Append an audit row (never raises to the caller path)."""
    await asyncio.to_thread(_record_audit_sync, user_id, event, detail)


__all__ = [
    "AUDIT_PREFIX",
    "KEY_PREFIX",
    "SESSION_PREFIX",
    "USAGE_PREFIX",
    "USER_PREFIX",
    "QuotaExceededError",
    "UsernameTakenError",
    "audit_key",
    "create_api_key",
    "create_session",
    "create_user",
    "current_month",
    "delete_session",
    "get_api_key",
    "get_session",
    "get_usage",
    "get_user",
    "key_key",
    "list_api_keys",
    "record_audit",
    "reserve_request_quota",
    "revoke_api_key",
    "session_key",
    "usage_key",
    "user_key",
]
