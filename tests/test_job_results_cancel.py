"""Tests for result retrieval, job cancellation and the optional webhook.

Covers the three Phase-0 API extensions:

* ``webhook_url`` is optional (dashboard/polling consumers) while remaining
  fully backward compatible when supplied;
* ``GET /v1/quantum/jobs/{id}/result`` — readiness conflicts, terminal-state
  payloads, pre-signed downloads and artifact failure mapping;
* ``POST /v1/quantum/jobs/{id}/cancel`` — atomic cancel transitions and the
  404/409 semantics around unknown or already-terminal jobs.
"""

from __future__ import annotations

import uuid

import pytest
from conftest import BELL_QASM3
from fastapi.testclient import TestClient

from api import results_service
from api.schemas import JobStatus


def _submit(client: TestClient, **overrides) -> dict:
    payload = {
        "circuit_data": BELL_QASM3,
        "format": "qasm3",
        "shots": 1024,
        "webhook_url": "https://consumer.example.com/hook",
    }
    payload.update(overrides)
    response = client.post("/v1/quantum/simulate", json=payload)
    return {"status": response.status_code, "body": response.json()}


def _completed_record(store: dict, job_id: str, *, key: str | None = None) -> str:
    """Drive a stubbed job into ``completed`` and return its artifact key."""
    artifact = key if key is not None else f"results/2026/10/19/{job_id}.json"
    store[job_id]["status"] = JobStatus.COMPLETED.value
    store[job_id]["result_s3_key"] = artifact
    store[job_id]["completed_at"] = "2026-10-19T00:00:07+00:00"
    return artifact


class TestOptionalWebhook:
    def test_submit_without_webhook_key_returns_202(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        # The key is absent entirely, not sent as null.
        response = client.post(
            "/v1/quantum/simulate",
            json={"circuit_data": BELL_QASM3, "format": "qasm3", "shots": 1024},
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert stubbed_backend["store"][job_id]["webhook_url"] is None
        # The worker envelope must also tolerate the absent callback.
        assert stubbed_backend["store"][job_id]["payload"]

    def test_submit_with_webhook_still_records_it(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        assert (
            stubbed_backend["store"][job_id]["webhook_url"]
            == "https://consumer.example.com/hook"
        )

    def test_null_webhook_is_accepted_explicitly(self, client: TestClient) -> None:
        result = _submit(client, webhook_url=None)
        assert result["status"] == 202

    def test_credentialled_webhook_still_rejected(self, client: TestClient) -> None:
        result = _submit(client, webhook_url="https://user:pass@consumer.example.com/hook")
        assert result["status"] == 422


class TestResultEndpoint:
    def test_unknown_job_404(self, client: TestClient) -> None:
        response = client.get(f"/v1/quantum/jobs/{uuid.uuid4()}/result")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "JOB_NOT_FOUND"

    def test_malformed_job_id_404(self, client: TestClient) -> None:
        assert client.get("/v1/quantum/jobs/not-a-uuid/result").status_code == 404

    def test_queued_job_conflicts(self, client: TestClient, stubbed_backend: dict) -> None:
        job_id = _submit(client)["body"]["job_id"]
        response = client.get(f"/v1/quantum/jobs/{job_id}/result")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error"] == "JOB_NOT_READY"
        assert detail["status"] == "queued"

    def test_processing_job_conflicts(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        stubbed_backend["store"][job_id]["status"] = JobStatus.PROCESSING.value
        assert client.get(f"/v1/quantum/jobs/{job_id}/result").status_code == 409

    def test_failed_job_returns_error_without_result(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        store = stubbed_backend["store"]
        store[job_id]["status"] = JobStatus.FAILED.value
        store[job_id]["error_message"] = "SimulationExecutionError: boom"

        response = client.get(f"/v1/quantum/jobs/{job_id}/result")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["result"] is None
        assert body["download_url"] is None
        assert "boom" in body["error_message"]

    def test_cancelled_job_returns_cancelled_state(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        stubbed_backend["store"][job_id]["status"] = JobStatus.CANCELLED.value

        body = client.get(f"/v1/quantum/jobs/{job_id}/result").json()
        assert body["status"] == "cancelled"
        assert body["result"] is None

    def test_completed_job_returns_counts_and_presigned_url(
        self,
        client: TestClient,
        stubbed_backend: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        artifact = _completed_record(stubbed_backend["store"], job_id)
        fetched: list[str] = []

        async def fake_fetch(key: str) -> dict:
            fetched.append(key)
            return {
                "shots": 1024,
                "counts": {"00": 512, "11": 512},
                "most_frequent": "00",
                "method": "matrix_product_state",
                "num_qubits": 2,
                "depth": 3,
                "simulation_time_ms": 842,
                "metadata": {"transpile_optimization_level": 2},
            }

        async def fake_presign(key: str) -> str:
            assert key == artifact
            return "https://storage.example/results.json?X-Amz-Signature=abc"

        monkeypatch.setattr(results_service, "fetch_result_document", fake_fetch)
        monkeypatch.setattr(results_service, "presign_result_url", fake_presign)

        response = client.get(f"/v1/quantum/jobs/{job_id}/result")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert fetched == [artifact]
        assert body["result"]["counts"] == {"00": 512, "11": 512}
        assert body["result"]["shots"] == 1024
        assert body["result"]["method"] == "matrix_product_state"
        assert body["result"]["most_frequent"] == "00"
        assert body["result"]["metadata"]["transpile_optimization_level"] == 2
        assert body["download_url"].startswith("https://storage.example/")
        assert body["download_expires_in_seconds"] == (
            results_service.PRESIGNED_URL_TTL_SECONDS
        )

    def test_completed_without_artifact_key_503(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        stubbed_backend["store"][job_id]["status"] = JobStatus.COMPLETED.value
        response = client.get(f"/v1/quantum/jobs/{job_id}/result")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "RESULT_UNAVAILABLE"

    def test_artifact_fetch_failure_maps_to_503(
        self, client: TestClient, stubbed_backend: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        _completed_record(stubbed_backend["store"], job_id)

        async def failing_fetch(key: str) -> dict:
            raise results_service.ResultUnavailable("s3 unreachable")

        monkeypatch.setattr(results_service, "fetch_result_document", failing_fetch)
        response = client.get(f"/v1/quantum/jobs/{job_id}/result")
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert detail["error"] == "RESULT_UNAVAILABLE"
        assert "unreachable" in detail["message"]

    def test_missing_artifact_maps_to_503(
        self, client: TestClient, stubbed_backend: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        _completed_record(stubbed_backend["store"], job_id)

        async def missing_fetch(key: str) -> dict:
            raise results_service.ResultNotFound(key)

        monkeypatch.setattr(results_service, "fetch_result_document", missing_fetch)
        response = client.get(f"/v1/quantum/jobs/{job_id}/result")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "RESULT_UNAVAILABLE"


class TestCancelEndpoint:
    def test_queued_job_cancels(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        response = client.post(f"/v1/quantum/jobs/{job_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == job_id
        assert body["status"] == "cancelled"
        assert stubbed_backend["store"][job_id]["status"] == "cancelled"
        assert stubbed_backend["calls"]["cancel"] == [job_id]
        # The tracking endpoint reflects the new state.
        assert client.get(f"/v1/quantum/jobs/{job_id}").json()["status"] == "cancelled"

    def test_processing_job_cancels(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        stubbed_backend["store"][job_id]["status"] = JobStatus.PROCESSING.value
        assert client.post(f"/v1/quantum/jobs/{job_id}/cancel").status_code == 200

    def test_unknown_job_404(self, client: TestClient) -> None:
        response = client.post(f"/v1/quantum/jobs/{uuid.uuid4()}/cancel")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "JOB_NOT_FOUND"

    def test_malformed_job_id_404(self, client: TestClient) -> None:
        assert client.post("/v1/quantum/jobs/not-a-uuid/cancel").status_code == 404

    def test_completed_job_not_cancellable_409(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        _completed_record(stubbed_backend["store"], job_id)

        response = client.post(f"/v1/quantum/jobs/{job_id}/cancel")
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error"] == "JOB_NOT_CANCELLABLE"
        assert detail["current_status"] == "completed"
        # Terminal state must be preserved.
        assert stubbed_backend["store"][job_id]["status"] == "completed"

    def test_double_cancel_is_conflict(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        job_id = _submit(client)["body"]["job_id"]
        assert client.post(f"/v1/quantum/jobs/{job_id}/cancel").status_code == 200
        second = client.post(f"/v1/quantum/jobs/{job_id}/cancel")
        assert second.status_code == 409
        assert second.json()["detail"]["current_status"] == "cancelled"
