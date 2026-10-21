"""Authentication primitives for the Quac API (P1).

Two credential types resolve to the same :class:`Identity`:

* **API keys** — ``qk_<key_id>_<secret>`` in the ``X-API-Key`` header. Only an
  HMAC-SHA256 hash of the secret (peppered with ``QUAC_SECRET_KEY``) is
  stored; the secret itself is shown exactly once at creation.
* **Session cookies** — ``quac_session`` set at register/login. The cookie
  value is a random token; the store keeps only its SHA-256, so a table dump
  cannot be replayed as a live session.

Password hashing is argon2id (argon2-cffi), OWASP-recommended parameters.
Failed authentication always yields the same generic 401 envelope — no
username oracle, no key-vs-cookie distinguisher.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request, Response, status

from . import identity
from .plans import PlanLimit, get_plan
from .ratelimit import identity_rate_limiter

logger = logging.getLogger("quac.api.auth")

SESSION_COOKIE_NAME: Final[str] = "quac_session"
SESSION_TTL_SECONDS: Final[int] = int(os.getenv("QUAC_SESSION_TTL", str(7 * 24 * 3600)))
COOKIE_SECURE: Final[bool] = os.getenv("QUAC_COOKIE_SECURE", "0") == "1"

API_KEY_SCHEME: Final[str] = "qk"
"""Key format: ``qk_<16-hex key_id>_<43-char urlsafe secret>``."""

# argon2id with OWASP-recommended parameters (m=19456 KiB, t=2, p=1). The
# parameters are embedded in every hash string, so raising them later never
# invalidates existing hashes.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


_ephemeral_pepper: bytes | None = None


def _pepper() -> bytes:
    """HMAC pepper for API-key secrets.

    Falls back to one random per-*process* value when ``QUAC_SECRET_KEY`` is
    unset: keys minted in that state die with the process (dev convenience),
    and production always sets the variable (terraform ``secret_key``).
    The fallback is memoised — a fresh random per call would make every
    verification fail.
    """
    global _ephemeral_pepper
    configured = os.getenv("QUAC_SECRET_KEY", "")
    if configured:
        return configured.encode("utf-8")
    if _ephemeral_pepper is None:
        logger.warning(
            "QUAC_SECRET_KEY is not set; using an ephemeral API-key pepper "
            "(keys will not survive a restart)"
        )
        _ephemeral_pepper = secrets.token_bytes(32)
    return _ephemeral_pepper


def hash_password(password: str) -> str:
    """Hash a password with argon2id."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verify a password against its argon2id hash (constant-failure shape)."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_api_secret(secret: str) -> str:
    """Peppered HMAC-SHA256 of an API-key secret (hex)."""
    return hmac.new(_pepper(), secret.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_api_secret(secret_hash: str, secret: str) -> bool:
    """Constant-time comparison of a presented secret against the stored hash."""
    return hmac.compare_digest(secret_hash, hash_api_secret(secret))


def mint_api_key() -> tuple[str, str, str, str]:
    """Mint ``(key_id, secret, full_token, prefix)`` for a new API key.

    ``full_token`` is what the user receives once; ``prefix`` (``qk_<key_id>``)
    is the safe-to-display identifier stored with the key row.
    """
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    full_token = f"{API_KEY_SCHEME}_{key_id}_{secret}"
    prefix = f"{API_KEY_SCHEME}_{key_id}"
    return key_id, secret, full_token, prefix


def parse_api_key(raw: str) -> tuple[str, str] | None:
    """Split ``qk_<key_id>_<secret>``; ``None`` when the shape is wrong."""
    parts = raw.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_SCHEME or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def mint_session_token() -> tuple[str, str]:
    """Mint ``(raw_token, sha256_hex)`` — only the hash is stored."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    return raw_token, token_hash


def session_token_hash(raw_token: str) -> str:
    """SHA-256 of a raw session token (lookup key)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def set_session_cookie(response: Response, raw_token: str) -> None:
    """Attach the session cookie (HttpOnly, SameSite=Lax, Secure in prod)."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie on logout."""
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )


# --------------------------------------------------------------------------------------
# Authenticated principal
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """The authenticated principal behind a request."""

    user_id: str
    username: str
    plan: PlanLimit
    auth_method: str  # "api_key" | "session"
    key_id: str | None = None  # present for api_key auth


def _unauthenticated() -> HTTPException:
    """Generic 401 — one shape for every failure mode (no username oracle)."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "error": "UNAUTHENTICATED",
            "message": "a valid API key (X-API-Key) or session cookie is required",
        },
        headers={"WWW-Authenticate": "ApiKey"},
    )


async def require_identity(request: Request) -> Identity:
    """FastAPI dependency: authenticate and enforce the plan rate limit.

    Resolution order: ``X-API-Key`` header, then ``quac_session`` cookie.
    Every failure mode — missing, malformed, unknown, revoked, expired —
    returns the same generic 401 envelope.
    """
    raw_key = request.headers.get("x-api-key")
    if raw_key:
        parsed = parse_api_key(raw_key.strip())
        if parsed is None:
            raise _unauthenticated()
        key_id, secret = parsed
        record = await identity.get_api_key(key_id)
        if record is None or record.get("revoked"):
            raise _unauthenticated()
        if not verify_api_secret(str(record.get("secret_hash", "")), secret):
            raise _unauthenticated()
        user = await identity.get_user(str(record.get("owner_username", "")))
        if user is None:
            raise _unauthenticated()
        principal = Identity(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            plan=get_plan(str(user.get("plan", "free"))),
            auth_method="api_key",
            key_id=key_id,
        )
    else:
        raw_token = request.cookies.get(SESSION_COOKIE_NAME, "")
        if not raw_token:
            raise _unauthenticated()
        session = await identity.get_session(session_token_hash(raw_token))
        if session is None:
            raise _unauthenticated()
        user = await identity.get_user(str(session.get("username", "")))
        if user is None:
            raise _unauthenticated()
        principal = Identity(
            user_id=str(user["user_id"]),
            username=str(user["username"]),
            plan=get_plan(str(user.get("plan", "free"))),
            auth_method="session",
        )

    # Per-identity rate limit: keyed by API-key id or user id so one noisy
    # credential cannot exhaust another principal's budget.
    rate_key = f"key:{principal.key_id}" if principal.key_id else f"user:{principal.user_id}"
    allowed, retry_after = await identity_rate_limiter.check(
        rate_key, max_requests=principal.plan.requests_per_minute
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "RATE_LIMIT_EXCEEDED",
                "message": (
                    f"plan '{principal.plan.name}' allows "
                    f"{principal.plan.requests_per_minute} requests per minute"
                ),
                "retry_after_seconds": round(retry_after, 3),
            },
            headers={"Retry-After": f"{retry_after:.0f}"},
        )
    return principal


# --------------------------------------------------------------------------------------
# Audit trail (fire-and-forget)
# --------------------------------------------------------------------------------------

_audit_tasks: set[Any] = set()


def schedule_audit(user_id: str, event: str, detail: dict[str, Any]) -> None:
    """Append an audit row without blocking (or failing) the request.

    The task is held in a strong-reference set until it finishes so the
    event loop cannot garbage-collect it mid-flight; audit failures are
    logged, never surfaced to the caller.
    """

    async def _run() -> None:
        try:
            await identity.record_audit(user_id, event, detail)
        except Exception:
            logger.warning("audit write failed event=%s", event, exc_info=True)

    task = asyncio.create_task(_run())
    _audit_tasks.add(task)
    task.add_done_callback(_audit_tasks.discard)


__all__ = [
    "API_KEY_SCHEME",
    "COOKIE_SECURE",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL_SECONDS",
    "Identity",
    "clear_session_cookie",
    "hash_api_secret",
    "hash_password",
    "mint_api_key",
    "mint_session_token",
    "parse_api_key",
    "require_identity",
    "schedule_audit",
    "session_token_hash",
    "set_session_cookie",
    "verify_api_secret",
    "verify_password",
]
