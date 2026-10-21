"""Plan tiers and quota limits (P1).

Config-driven product limits — there is **no payment integration and no
billing module**: plans are static configuration describing what each tier
may do, assigned free at registration (see the tracker for any future
billing phase). Nothing here charges anyone anything; the numbers are
admission-control limits enforced by the API before side effects are created.

Enforcement points (see ``api.main``):

* ``max_qubits`` / ``max_shots`` — checked after the platform firewall, per
  tenant, before persistence (403 ``PLAN_LIMIT_EXCEEDED``).
* ``max_concurrent_jobs`` — owner's queued+processing count at submit time
  (429 ``TOO_MANY_ACTIVE_JOBS``).
* ``monthly_requests`` — atomic conditional counter incremented on each
  ``POST /simulate`` and ``POST /analyze`` (429 ``QUOTA_EXCEEDED``).
* ``monthly_compute_ms`` — admission check against the recorded usage of the
  current calendar month; in-flight jobs may overshoot slightly (documented).
* ``requests_per_minute`` — per-identity sliding-window rate limit.

Reads (tracking, result fetch, job list) are not counted against
``monthly_requests``; they are bounded by the per-identity rate limit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

DEFAULT_PLAN: Final[str] = os.getenv("QUAC_DEFAULT_PLAN", "free")
"""Plan assigned at registration (override for local experiments only)."""


@dataclass(frozen=True, slots=True)
class PlanLimit:
    """Admission-control limits for one plan tier."""

    name: str
    monthly_requests: int
    """Simulation submissions + pre-flight analyses per calendar month."""
    monthly_compute_ms: int
    """Recorded worker compute time per calendar month (admission-checked)."""
    max_concurrent_jobs: int
    """Queued + processing jobs allowed at submit time."""
    max_qubits: int
    """Widest circuit the plan may submit (<= the 28-qubit platform cap)."""
    max_shots: int
    """Largest shot count the plan may request (<= the 8192 platform cap)."""
    requests_per_minute: int
    """Per-identity sliding-window rate limit across /v1 routes."""


PLAN_LIMITS: Final[dict[str, PlanLimit]] = {
    "free": PlanLimit(
        name="free",
        monthly_requests=200,
        monthly_compute_ms=3_600_000,  # 1 compute-hour / month
        max_concurrent_jobs=2,
        max_qubits=12,
        max_shots=2048,
        requests_per_minute=60,
    ),
    "developer": PlanLimit(
        name="developer",
        monthly_requests=2_000,
        monthly_compute_ms=36_000_000,  # 10 compute-hours / month
        max_concurrent_jobs=8,
        max_qubits=20,
        max_shots=4096,
        requests_per_minute=180,
    ),
    "pro": PlanLimit(
        name="pro",
        monthly_requests=20_000,
        monthly_compute_ms=180_000_000,  # 50 compute-hours / month
        max_concurrent_jobs=32,
        max_qubits=28,
        max_shots=8192,
        requests_per_minute=600,
    ),
    "enterprise": PlanLimit(
        name="enterprise",
        monthly_requests=200_000,
        monthly_compute_ms=1_800_000_000,  # 500 compute-hours / month
        max_concurrent_jobs=128,
        max_qubits=28,
        max_shots=8192,
        requests_per_minute=1200,
    ),
}
"""The four plan tiers. Every limit is a real enforced ceiling."""


def get_plan(name: str) -> PlanLimit:
    """Resolve a plan tier; unknown names fall back to ``free``.

    A user row carrying a plan that no longer exists (renamed tier, manual
    edit) must never grant *more* than the default — falling back to the
    smallest tier is the safe direction.
    """
    fallback = DEFAULT_PLAN if DEFAULT_PLAN in PLAN_LIMITS else "free"
    return PLAN_LIMITS.get(name, PLAN_LIMITS[fallback])


__all__ = ["DEFAULT_PLAN", "PLAN_LIMITS", "PlanLimit", "get_plan"]
