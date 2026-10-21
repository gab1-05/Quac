"""Sliding-window rate limiting for the Quac API.

Two independent limiters share one implementation:

* ``rate_limiter`` — the *global* per-client-IP limiter guarding the job
  tracking surface (``QUAC_RATE_LIMIT_REQUESTS`` / ``QUAC_RATE_LIMIT_WINDOW``).
* ``identity_rate_limiter`` — the *per-identity* limiter enforcing each plan
  tier's ``requests_per_minute`` against the authenticated principal
  (API-key id or session user id), consulted through
  :func:`api.auth.identity_rate_limit`.

This module intentionally owns no FastAPI application state so that
``api.auth`` and ``api.main`` can both import it without a cycle.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import defaultdict, deque
from typing import Final

from fastapi import HTTPException, Request, Response, status

RATE_LIMIT_REQUESTS: Final[int] = int(os.getenv("QUAC_RATE_LIMIT_REQUESTS", "120"))
RATE_LIMIT_WINDOW_SECONDS: Final[float] = float(os.getenv("QUAC_RATE_LIMIT_WINDOW", "60"))


class SlidingWindowRateLimiter:
    """Per-key fixed-capacity sliding-window limiter.

    Deployed as a *global* FastAPI dependency so that every protected route
    draws from the same limiter instance within a process. Stale timestamps are
    pruned on access, and an asyncio lock serialises admissions so concurrent
    requests cannot over-admit past the window cap.

    ``check`` accepts an optional per-call ``max_requests`` override so the
    identity limiter can enforce each plan tier's own RPM from a single shared
    instance.
    """

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str, max_requests: int | None = None) -> tuple[bool, float]:
        """Record a hit for ``key``; return ``(allowed, retry_after_seconds)``.

        ``max_requests`` overrides the instance-wide cap for this call, letting
        plan-specific RPM limits share one limiter.
        """
        now = time.monotonic()
        cap = self._max_requests if max_requests is None else max_requests
        async with self._lock:
            bucket = self._hits[key]
            while bucket and now - bucket[0] >= self._window:
                bucket.popleft()
            if len(bucket) >= cap:
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
identity_rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def client_key(request: Request) -> str:
    """Prefer the earliest untrusted forwarding hop, else the socket peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


async def rate_limit(request: Request, response: Response) -> None:
    """FastAPI dependency enforcing the global per-client tracking rate limit."""
    allowed, retry_after = await rate_limiter.check(client_key(request))
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


__all__ = [
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "SlidingWindowRateLimiter",
    "client_key",
    "identity_rate_limiter",
    "rate_limit",
    "rate_limiter",
]
