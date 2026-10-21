"""Authentication and API-key routes (P1).

* ``POST /v1/auth/register`` — open registration onto the free plan; sets the
  session cookie so the console is logged in immediately.
* ``POST /v1/auth/login`` — session-cookie login (generic 401 on failure).
* ``POST /v1/auth/logout`` — deletes the session row and clears the cookie.
* ``GET  /v1/auth/me`` — the caller's identity, plan limits and month usage.
* ``POST /v1/keys`` — mint an API key (secret shown exactly once).
* ``GET  /v1/keys`` — list the caller's keys (never secrets).
* ``DELETE /v1/keys/{key_id}`` — revoke one of the caller's keys.

Register/login are additionally guarded by the global per-IP rate limiter to
slow credential stuffing; everything authenticated runs under the per-plan
RPM limit enforced inside :func:`api.auth.require_identity`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from . import auth, identity, plans
from .auth import Identity, require_identity
from .ratelimit import rate_limit
from .schemas import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyListResponse,
    ApiKeyRevokeResponse,
    ApiKeySummary,
    AuthUserResponse,
    LoginRequest,
    PlanLimitsResponse,
    RegisterRequest,
    UsageResponse,
)

logger = logging.getLogger("quac.api.auth_routes")

router = APIRouter(prefix="/v1", tags=["auth"])


def _plan_limits(plan: plans.PlanLimit) -> PlanLimitsResponse:
    """Project a PlanLimit onto its wire contract."""
    return PlanLimitsResponse(
        name=plan.name,
        monthly_requests=plan.monthly_requests,
        monthly_compute_ms=plan.monthly_compute_ms,
        max_concurrent_jobs=plan.max_concurrent_jobs,
        max_qubits=plan.max_qubits,
        max_shots=plan.max_shots,
        requests_per_minute=plan.requests_per_minute,
    )


async def _usage_or_default(user_id: str) -> UsageResponse:
    """Current-month counters, defaulting to zeroes before first use."""
    month = identity.current_month()
    row = await identity.get_usage(user_id, month)
    requests_used = int(row.get("requests", 0)) if row else 0
    compute_ms = int(row.get("compute_ms", 0)) if row else 0
    return UsageResponse(month=month, requests=requests_used, compute_ms=compute_ms)


def _auth_user_response(
    user_id: str, username: str, plan_name: str, created_at: str, usage: UsageResponse | None
) -> AuthUserResponse:
    """Build the shared auth response body."""
    return AuthUserResponse(
        user_id=user_id,
        username=username,
        plan=plan_name,
        created_at=created_at,
        limits=_plan_limits(plans.get_plan(plan_name)),
        usage=usage,
    )


@router.post(
    "/auth/register",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account (open registration, free plan)",
    dependencies=[Depends(rate_limit)],
    responses={
        201: {"description": "Account created; session cookie set"},
        409: {"description": "Username already taken"},
        429: {"description": "Global rate limit exceeded"},
    },
)
async def register(body: RegisterRequest, response: Response) -> AuthUserResponse:
    """Register a new user and start a session.

    The password is hashed with argon2id before the row is written. A
    duplicate username returns the same generic conflict whether or not the
    account exists — registration is public, so no stronger oracle is offered.
    """
    password_hash = await _hash_password_async(body.password)
    try:
        user = await identity.create_user(
            body.username, password_hash, plans.DEFAULT_PLAN
        )
    except identity.UsernameTakenError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "USERNAME_TAKEN", "message": "username is already registered"},
        ) from exc

    user_id = str(user["user_id"])
    raw_token, token_hash = auth.mint_session_token()
    await identity.create_session(
        token_hash, user_id, body.username, auth.SESSION_TTL_SECONDS
    )
    auth.set_session_cookie(response, raw_token)
    auth.schedule_audit(user_id, "user.register", {"username": body.username})
    logger.info("registration user_id=%s username=%s", user_id, body.username)
    return _auth_user_response(
        user_id, body.username, str(user["plan"]), str(user["created_at"]), None
    )


async def _hash_password_async(password: str) -> str:
    """argon2id hashing is CPU-bound; keep it off the event loop."""
    return await asyncio.to_thread(auth.hash_password, password)


@router.post(
    "/auth/login",
    response_model=AuthUserResponse,
    summary="Start a session (username + password)",
    dependencies=[Depends(rate_limit)],
    responses={
        200: {"description": "Session started; cookie set"},
        401: {"description": "Invalid credentials (generic envelope)"},
        429: {"description": "Global rate limit exceeded"},
    },
)
async def login(body: LoginRequest, response: Response) -> AuthUserResponse:
    """Verify credentials and set the session cookie.

    Unknown user and wrong password produce the identical 401; the unknown
    path still performs a dummy argon2 verify so response timing does not
    leak account existence.
    """
    user = await identity.get_user(body.username)
    stored_hash = str((user or {}).get("password_hash", ""))
    if user is None:
        # Dummy verify against a fixed invalid hash: same work, same failure.
        await _verify_password_async(
            "$argon2id$v=19$m=19456,t=2,p=1$YWFhYWFhYWFhYWFhYWFhYQ$"
            "0000000000000000000000000000000000000000000",
            body.password,
        )
        raise _invalid_credentials()
    if not await _verify_password_async(stored_hash, body.password):
        auth.schedule_audit(str(user["user_id"]), "user.login_failed", {"username": body.username})
        raise _invalid_credentials()

    raw_token, token_hash = auth.mint_session_token()
    await identity.create_session(
        token_hash, str(user["user_id"]), body.username, auth.SESSION_TTL_SECONDS
    )
    auth.set_session_cookie(response, raw_token)
    auth.schedule_audit(str(user["user_id"]), "user.login", {"username": body.username})
    return _auth_user_response(
        str(user["user_id"]),
        body.username,
        str(user.get("plan", "free")),
        str(user.get("created_at", "")),
        None,
    )


async def _verify_password_async(password_hash: str, password: str) -> bool:
    """Verify off the event loop (argon2id is CPU-bound)."""
    return await asyncio.to_thread(auth.verify_password, password_hash, password)


def _invalid_credentials() -> HTTPException:
    """Generic 401 for both unknown user and wrong password."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "INVALID_CREDENTIALS", "message": "invalid username or password"},
    )


@router.post(
    "/auth/logout",
    summary="End the current session",
    responses={200: {"description": "Session deleted; cookie cleared"}},
)
async def logout(request: Request, response: Response) -> dict[str, str]:
    """Delete the session row and clear the cookie (idempotent).

    API-key callers have no session; the call is a no-op success so clients
    can always hit logout on teardown.
    """
    raw_token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    if raw_token:
        await identity.delete_session(auth.session_token_hash(raw_token))
    auth.clear_session_cookie(response)
    return {"detail": "logged out"}


@router.get(
    "/auth/me",
    response_model=AuthUserResponse,
    summary="The authenticated identity, plan and usage",
)
async def me(
    identity_principal: Annotated[Identity, Depends(require_identity)],
) -> AuthUserResponse:
    """Return the caller's identity, plan ceilings and current-month usage."""
    user = await identity.get_user(identity_principal.username)
    usage = await _usage_or_default(identity_principal.user_id)
    return _auth_user_response(
        identity_principal.user_id,
        identity_principal.username,
        identity_principal.plan.name,
        str((user or {}).get("created_at", "")),
        usage,
    )


@router.post(
    "/keys",
    response_model=ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key (secret shown once)",
)
async def create_key(
    body: ApiKeyCreateRequest,
    identity_principal: Annotated[Identity, Depends(require_identity)],
) -> ApiKeyCreatedResponse:
    """Create an API key for the caller.

    The full ``qk_…`` token appears in this response only; the stored row
    keeps an HMAC-SHA256 hash peppered with ``QUAC_SECRET_KEY``.
    """
    key_id, secret, full_token, prefix = auth.mint_api_key()
    record = await identity.create_api_key(
        key_id,
        identity_principal.user_id,
        identity_principal.username,
        auth.hash_api_secret(secret),
        prefix,
        body.label,
    )
    auth.schedule_audit(
        identity_principal.user_id,
        "key.create",
        {"key_id": key_id, "label": body.label},
    )
    return ApiKeyCreatedResponse(
        key_id=key_id,
        key=full_token,
        prefix=prefix,
        label=body.label,
        created_at=str(record["created_at"]),
    )


@router.get(
    "/keys",
    response_model=ApiKeyListResponse,
    summary="List the caller's API keys",
)
async def list_keys(
    identity_principal: Annotated[Identity, Depends(require_identity)],
) -> ApiKeyListResponse:
    """List the caller's API keys (metadata only — never secrets)."""
    rows = await identity.list_api_keys(identity_principal.user_id)
    return ApiKeyListResponse(
        keys=[
            ApiKeySummary(
                key_id=str(row.get("key_id", "")),
                prefix=str(row.get("prefix", "")),
                label=str(row.get("label", "default")),
                created_at=str(row.get("created_at", "")),
                revoked=bool(row.get("revoked", False)),
            )
            for row in rows
        ]
    )


@router.delete(
    "/keys/{key_id}",
    response_model=ApiKeyRevokeResponse,
    summary="Revoke an API key",
    responses={404: {"description": "Unknown key id (or not the caller's)"}},
)
async def revoke_key(
    key_id: str,
    identity_principal: Annotated[Identity, Depends(require_identity)],
) -> ApiKeyRevokeResponse:
    """Revoke one of the caller's keys.

    A key owned by another tenant returns the same 404 as a missing key —
    key ids are not confirmed across tenant boundaries.
    """
    revoked = await identity.revoke_api_key(identity_principal.user_id, key_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "KEY_NOT_FOUND", "message": f"unknown key id {key_id!r}"},
        )
    auth.schedule_audit(
        identity_principal.user_id, "key.revoke", {"key_id": key_id}
    )
    return ApiKeyRevokeResponse(key_id=key_id)


__all__ = ["router"]
