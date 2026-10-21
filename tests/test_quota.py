"""Plan limits and quota enforcement tests (P1).

Gate order under test (see ``api.main.submit_simulation``):

    firewall (422) → plan qubit/shot (403 PLAN_LIMIT_EXCEEDED)
    → compute admission (429 QUOTA_EXCEEDED) → concurrent cap (429
    TOO_MANY_ACTIVE_JOBS) → monthly request counter (429 QUOTA_EXCEEDED)

Plus: analyze counts against the monthly request quota only (never plan
qubit/shot ceilings), and the per-plan per-minute rate limit.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api import identity, plans


def _qasm3(n_qubits: int) -> str:
    """A valid n-qubit QASM3 circuit (H on each qubit, no coupling)."""
    lines = ["OPENQASM 3.0;", 'include "stdgates.inc";', f"qubit[{n_qubits}] q;"]
    lines += [f"h q[{i}];" for i in range(n_qubits)]
    return "\n".join(lines) + "\n"


def _client_for(identity_store: dict, plan: str, username: str | None = None) -> TestClient:
    """A TestClient authenticated as a fresh user on ``plan``."""
    from api.main import app

    user = identity_store["add_user"](username or f"q-{uuid.uuid4().hex[:8]}", plan=plan)
    api_key = identity_store["add_key"](user)
    client = TestClient(app, headers={"X-API-Key": api_key})
    client.auth_user = user  # type: ignore[attr-defined]
    return client


class TestPlanQubitShotLimits:
    def test_free_plan_qubit_ceiling_403(self, stubbed_backend: dict) -> None:
        # 13 qubits clears the 28-qubit platform firewall, then trips the plan.
        with _client_for(stubbed_backend["identity"], "free") as client:
            response = client.post(
                "/v1/quantum/simulate",
                json={"circuit_data": _qasm3(13), "format": "qasm3", "shots": 1024},
            )
        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["error"] == "PLAN_LIMIT_EXCEEDED"
        assert detail["observed_qubits"] == 13
        assert detail["plan_max_qubits"] == 12
        # Admission rejected before persistence.
        assert stubbed_backend["calls"]["create"] == []
        assert stubbed_backend["calls"]["enqueue"] == []

    def test_free_plan_shot_ceiling_403(self, stubbed_backend: dict) -> None:
        # 4096 shots clears the 8192 platform firewall, then trips the plan.
        with _client_for(stubbed_backend["identity"], "free") as client:
            response = client.post(
                "/v1/quantum/simulate",
                json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 4096},
            )
        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "PLAN_LIMIT_EXCEEDED"
        assert response.json()["detail"]["plan_max_shots"] == 2048

    def test_free_plan_within_limits_accepted(self, stubbed_backend: dict) -> None:
        with _client_for(stubbed_backend["identity"], "free") as client:
            response = client.post(
                "/v1/quantum/simulate",
                json={"circuit_data": _qasm3(12), "format": "qasm3", "shots": 2048},
            )
        assert response.status_code == 202
        assert len(stubbed_backend["calls"]["enqueue"]) == 1


class TestConcurrencyCap:
    def test_too_many_active_jobs_429(self, stubbed_backend: dict) -> None:
        identity_store = stubbed_backend["identity"]
        with _client_for(identity_store, "free") as client:
            owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
            # Seed two active jobs for this owner (free plan allows 2).
            for _ in range(2):
                stubbed_backend["store"][str(uuid.uuid4())] = {
                    "job_id": "seed",
                    "status": "queued",
                    "entity": "job",
                    "owner_id": owner,
                    "created_at": "2026-10-19T00:00:00+00:00",
                }
            response = client.post(
                "/v1/quantum/simulate",
                json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 1024},
            )
        assert response.status_code == 429
        detail = response.json()["detail"]
        assert detail["error"] == "TOO_MANY_ACTIVE_JOBS"
        assert detail["active_jobs"] == 2
        assert detail["limit"] == 2
        assert stubbed_backend["calls"]["create"] == []

    def test_terminal_jobs_do_not_count_towards_cap(self, stubbed_backend: dict) -> None:
        identity_store = stubbed_backend["identity"]
        with _client_for(identity_store, "free") as client:
            owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
            for _ in range(3):
                stubbed_backend["store"][str(uuid.uuid4())] = {
                    "job_id": "seed",
                    "status": "completed",
                    "entity": "job",
                    "owner_id": owner,
                    "created_at": "2026-10-19T00:00:00+00:00",
                }
            response = client.post(
                "/v1/quantum/simulate",
                json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 1024},
            )
        assert response.status_code == 202


class TestMonthlyRequestQuota:
    def test_exhausted_request_quota_429(self, stubbed_backend: dict) -> None:
        identity_store = stubbed_backend["identity"]
        with _client_for(identity_store, "free") as client:
            owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
            month = identity.current_month()
            identity_store["usage"][(owner, month)] = {
                "requests": plans.PLAN_LIMITS["free"].monthly_requests,
                "compute_ms": 0,
            }
            response = client.post(
                "/v1/quantum/simulate",
                json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 1024},
            )
        assert response.status_code == 429
        detail = response.json()["detail"]
        assert detail["error"] == "QUOTA_EXCEEDED"
        assert detail["quota"] == "requests"
        assert stubbed_backend["calls"]["create"] == []
        assert stubbed_backend["calls"]["enqueue"] == []

    def test_request_counter_increments_per_submit(self, stubbed_backend: dict) -> None:
        identity_store = stubbed_backend["identity"]
        with _client_for(identity_store, "free") as client:
            owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
            for _ in range(3):
                response = client.post(
                    "/v1/quantum/simulate",
                    json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 1024},
                )
                assert response.status_code == 202
                # Close the job so the free plan's 2-concurrent cap stays clear.
                store = stubbed_backend["store"]
                store[response.json()["job_id"]]["status"] = "completed"
            row = identity_store["usage"][(owner, identity.current_month())]
        assert row["requests"] == 3

    def test_analyze_counts_against_request_quota(self, stubbed_backend: dict) -> None:
        identity_store = stubbed_backend["identity"]
        with _client_for(identity_store, "free") as client:
            owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
            month = identity.current_month()
            free = plans.PLAN_LIMITS["free"]
            identity_store["usage"][(owner, month)] = {
                "requests": free.monthly_requests - 1,
                "compute_ms": 0,
            }
            ok = client.post(
                "/v1/quantum/analyze",
                json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 1024},
            )
            assert ok.status_code == 200
            exhausted = client.post(
                "/v1/quantum/analyze",
                json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 1024},
            )
        assert exhausted.status_code == 429
        assert exhausted.json()["detail"]["error"] == "QUOTA_EXCEEDED"

    def test_analyze_ignores_plan_qubit_and_shot_ceilings(self, stubbed_backend: dict) -> None:
        """Analysis exists to check big circuits — plan caps gate submit only."""
        with _client_for(stubbed_backend["identity"], "free") as client:
            response = client.post(
                "/v1/quantum/analyze",
                # 27 qubits + 8192 shots: far above the free plan's 12/2048.
                json={"circuit_data": _qasm3(27), "format": "qasm3", "shots": 8192},
            )
        assert response.status_code == 200
        assert response.json()["num_qubits"] == 27


class TestComputeQuota:
    def test_exhausted_compute_budget_429(self, stubbed_backend: dict) -> None:
        identity_store = stubbed_backend["identity"]
        with _client_for(identity_store, "free") as client:
            owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
            month = identity.current_month()
            identity_store["usage"][(owner, month)] = {
                "requests": 1,
                "compute_ms": plans.PLAN_LIMITS["free"].monthly_compute_ms,
            }
            response = client.post(
                "/v1/quantum/simulate",
                json={"circuit_data": _qasm3(2), "format": "qasm3", "shots": 1024},
            )
        assert response.status_code == 429
        detail = response.json()["detail"]
        assert detail["error"] == "QUOTA_EXCEEDED"
        assert detail["quota"] == "compute_ms"
        assert stubbed_backend["calls"]["create"] == []


class TestPlanRateLimit:
    def test_per_plan_rpm_trips_429(
        self, stubbed_backend: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Drop the free plan to 1 request/minute so two calls trip the limiter.
        monkeypatch.setitem(
            plans.PLAN_LIMITS,
            "free",
            replace(plans.PLAN_LIMITS["free"], requests_per_minute=1),
        )
        with _client_for(stubbed_backend["identity"], "free") as client:
            first = client.get("/v1/auth/me")
            second = client.get("/v1/auth/me")
        assert first.status_code == 200
        assert second.status_code == 429
        detail = second.json()["detail"]
        assert detail["error"] == "RATE_LIMIT_EXCEEDED"
        assert "Retry-After" in second.headers
