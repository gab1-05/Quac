"""Tenant isolation tests (P1).

A second authenticated user must observe nothing about the first user's
jobs: every cross-tenant read (status, result, cancel) maps to the exact
same 404 envelope as a missing id, and listings never leak foreign rows.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _submit(client: TestClient, bell_qasm3: str) -> str:
    response = client.post(
        "/v1/quantum/simulate",
        json={"circuit_data": bell_qasm3, "format": "qasm3", "shots": 1024},
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _second_client(identity_store: dict) -> TestClient:
    """A second tenant (fresh enterprise user + key)."""
    from api.main import app

    user = identity_store["add_user"](f"intruder-{uuid.uuid4().hex[:8]}")
    api_key = identity_store["add_key"](user)
    client = TestClient(app, headers={"X-API-Key": api_key})
    client.auth_user = user  # type: ignore[attr-defined]
    return client


class TestCrossTenantJobReads:
    def test_status_is_404_identical_to_missing(
        self, client: TestClient, identity_store: dict, bell_qasm3: str
    ) -> None:
        job_id = _submit(client, bell_qasm3)
        with _second_client(identity_store) as intruder:
            foreign = intruder.get(f"/v1/quantum/jobs/{job_id}")
            missing = intruder.get(f"/v1/quantum/jobs/{uuid.uuid4()}")
        # Identical envelopes (modulo the echoed id): existence never leaks.
        assert foreign.status_code == missing.status_code == 404
        fdet, mdet = foreign.json()["detail"], missing.json()["detail"]
        assert fdet["error"] == mdet["error"] == "JOB_NOT_FOUND"
        assert fdet["message"] == f"unknown job id {job_id!r}"
        assert mdet["message"].startswith("unknown job id ")

    def test_owner_still_sees_own_job(self, client: TestClient, bell_qasm3: str) -> None:
        job_id = _submit(client, bell_qasm3)
        assert client.get(f"/v1/quantum/jobs/{job_id}").status_code == 200

    def test_result_is_404_for_foreign_tenant(
        self, client: TestClient, identity_store: dict, bell_qasm3: str
    ) -> None:
        job_id = _submit(client, bell_qasm3)
        with _second_client(identity_store) as intruder:
            response = intruder.get(f"/v1/quantum/jobs/{job_id}/result")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "JOB_NOT_FOUND"

    def test_cancel_is_404_and_job_survives(
        self, client: TestClient, identity_store: dict, bell_qasm3: str
    ) -> None:
        job_id = _submit(client, bell_qasm3)
        with _second_client(identity_store) as intruder:
            response = intruder.post(f"/v1/quantum/jobs/{job_id}/cancel")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "JOB_NOT_FOUND"
        # The victim's job is untouched (still queued, not cancelled).
        own = client.get(f"/v1/quantum/jobs/{job_id}").json()
        assert own["status"] == "queued"


class TestCrossTenantListing:
    def test_listing_shows_only_own_jobs(
        self, client: TestClient, identity_store: dict, bell_qasm3: str
    ) -> None:
        mine = _submit(client, bell_qasm3)
        with _second_client(identity_store) as intruder:
            _submit(intruder, bell_qasm3)  # intruder has a job of its own
            listing = intruder.get("/v1/quantum/jobs")
        assert listing.status_code == 200
        ids = [row["job_id"] for row in listing.json()["jobs"]]
        assert mine not in ids
        assert len(ids) == 1

    def test_owner_listing_includes_own_job(
        self, client: TestClient, bell_qasm3: str
    ) -> None:
        job_id = _submit(client, bell_qasm3)
        listing = client.get("/v1/quantum/jobs")
        assert [row["job_id"] for row in listing.json()["jobs"]] == [job_id]


class TestOwnershipStampedOnCreate:
    def test_created_record_carries_owner_and_entity(
        self, client: TestClient, stubbed_backend: dict, bell_qasm3: str
    ) -> None:
        job_id = _submit(client, bell_qasm3)
        record = stubbed_backend["store"][job_id]
        assert record["owner_id"] == client.auth_user["user_id"]  # type: ignore[attr-defined]
        assert record["entity"] == "job"
        assert record["schema_version"] == 1
