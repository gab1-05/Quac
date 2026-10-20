"""FastAPI endpoint tests: submit, track, rate limiting, error envelopes."""

from __future__ import annotations

import time
import uuid

import pytest
from conftest import BELL_QASM3, QASM3_29_QUBITS
from fastapi.testclient import TestClient

from api.main import rate_limiter
from api.schemas import CircuitFormat, JobStatus


def _submit(client: TestClient, **overrides) -> dict:
    payload = {
        "circuit_data": BELL_QASM3,
        "format": "qasm3",
        "shots": 1024,
        "webhook_url": "https://consumer.example.com/hook",
    }
    payload.update(overrides)
    response = client.post("/v1/quantum/simulate", json=payload)
    return {"status": response.status_code, "body": response.json(), "raw": response}


class TestSubmitEndpoint:
    def test_accept_returns_202_with_job_id(self, client: TestClient) -> None:
        result = _submit(client)
        assert result["status"] == 202
        body = result["body"]
        assert body["status"] == JobStatus.QUEUED.value
        uuid.UUID(body["job_id"], version=4)  # strict UUIDv4 shape
        assert body["status_url"] == f"/v1/quantum/jobs/{body['job_id']}"

    def test_persistence_and_enqueue_order(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        calls = stubbed_backend["calls"]
        # Store first, then queue — the validated payload only reaches SQS
        # after a durable record exists.
        assert calls["create"] == [job_id]
        assert calls["enqueue"] == [(job_id, 2)]
        assert stubbed_backend["store"][job_id]["status"] == "queued"

    def test_29_qubits_rejected_before_any_side_effect(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        result = _submit(client, circuit_data=QASM3_29_QUBITS)
        assert result["status"] == 422
        detail = result["body"]["detail"]
        assert detail["error"] == "QUBIT_LIMIT_EXCEEDED"
        assert detail["observed_qubits"] == 29
        assert detail["max_qubits"] == 28
        # Firewall rejection must have zero infrastructure footprint.
        assert stubbed_backend["calls"]["create"] == []
        assert stubbed_backend["calls"]["enqueue"] == []

    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (
                {"circuit_data": "OPENQASM 3.0;\ninclude </etc/passwd>;\nqubit[1] q;\n"},
                "FILE_INCLUDE_BANNED",
            ),
            ({"shots": 99999}, None),  # pydantic-level 422
        ],
    )
    def test_rejections_have_no_side_effects(
        self, client: TestClient, stubbed_backend: dict, payload: dict, code: str | None
    ) -> None:
        result = _submit(client, **payload)
        assert result["status"] == 422
        if code is not None:
            assert result["body"]["detail"]["error"] == code
        assert stubbed_backend["calls"]["create"] == []
        assert stubbed_backend["calls"]["enqueue"] == []

    def test_queue_failure_marks_job_failed_and_returns_503(
        self, client: TestClient, stubbed_backend: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from api import queue_service

        async def failing_enqueue(job_id, request, qubit_count):
            raise queue_service.QueuePublishError("broker unreachable")

        monkeypatch.setattr(queue_service, "enqueue_simulation", failing_enqueue)
        result = _submit(client)
        assert result["status"] == 503
        assert result["body"]["detail"]["error"] == "QUEUE_UNAVAILABLE"
        (job_id, status) = stubbed_backend["calls"]["update"][0]
        assert status == "failed"
        assert stubbed_backend["store"][job_id]["status"] == "failed"


class TestJobTrackingEndpoint:
    def test_returns_record(self, client: TestClient, stubbed_backend: dict) -> None:
        job_id = _submit(client)["body"]["job_id"]
        response = client.get(f"/v1/quantum/jobs/{job_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == job_id
        assert body["status"] == "queued"
        assert body["created_at"]
        assert body["completed_at"] is None
        assert body["error_message"] is None

    def test_unknown_job_404(self, client: TestClient) -> None:
        response = client.get(f"/v1/quantum/jobs/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "JOB_NOT_FOUND"

    def test_malformed_job_id_404(self, client: TestClient) -> None:
        response = client.get("/v1/quantum/jobs/not-a-uuid")
        assert response.status_code == 404

    def test_failed_job_surfaces_error_message(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        store = stubbed_backend["store"]
        store[job_id]["status"] = "failed"
        store[job_id]["error_message"] = "SimulationExecutionError: boom"
        store[job_id]["completed_at"] = "2026-10-19T00:00:05+00:00"

        body = client.get(f"/v1/quantum/jobs/{job_id}").json()
        assert body["status"] == "failed"
        assert "boom" in body["error_message"]
        assert body["completed_at"] == "2026-10-19T00:00:05+00:00"


class TestRateLimiter:
    @pytest.fixture(autouse=True)
    def _isolate_limiter(self):
        rate_limiter._max_requests = 1000
        import asyncio

        asyncio.run(rate_limiter.reset())
        yield
        rate_limiter._max_requests = 1000
        asyncio.run(rate_limiter.reset())

    def test_tracking_endpoint_enforces_global_limit(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        rate_limiter._max_requests = 3

        statuses = [
            client.get(f"/v1/quantum/jobs/{job_id}").status_code for _ in range(5)
        ]
        assert statuses == [200, 200, 200, 429, 429]

        blocked = client.get(f"/v1/quantum/jobs/{job_id}")
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        detail = blocked.json()["detail"]
        assert detail["error"] == "RATE_LIMIT_EXCEEDED"
        assert detail["retry_after_seconds"] > 0

    def test_limit_recovers_after_window(self, client: TestClient) -> None:
        job_id = _submit(client)["body"]["job_id"]
        rate_limiter._max_requests = 1
        rate_limiter._window = 0.2

        assert client.get(f"/v1/quantum/jobs/{job_id}").status_code == 200
        assert client.get(f"/v1/quantum/jobs/{job_id}").status_code == 429
        time.sleep(0.25)
        assert client.get(f"/v1/quantum/jobs/{job_id}").status_code == 200


class TestOpsEndpoints:
    def test_healthz(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_formats_advertisement(self, client: TestClient) -> None:
        body = client.get("/v1/quantum/formats").json()
        assert set(body["formats"]) == {member.value for member in CircuitFormat}
        assert body["max_qubits"] == 28
        assert body["max_shots"] == 8192
