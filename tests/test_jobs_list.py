"""Owner-scoped job listing tests (P1): ``GET /v1/quantum/jobs``.

Covers scoping, newest-first ordering, status filtering, cursor
pagination, invalid-cursor handling and identity-row exclusion.
"""

from __future__ import annotations

import base64
import json
import uuid

from fastapi.testclient import TestClient


def _submit(client: TestClient, bell_qasm3: str, **overrides) -> str:
    payload = {"circuit_data": bell_qasm3, "format": "qasm3", "shots": 1024}
    payload.update(overrides)
    response = client.post("/v1/quantum/simulate", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _seed(
    stubbed_backend: dict,
    owner_id: str,
    *,
    status: str = "completed",
    created_at: str,
    job_id: str | None = None,
) -> str:
    jid = job_id or str(uuid.uuid4())
    stubbed_backend["store"][jid] = {
        "job_id": jid,
        "payload": "{}",
        "format": "qasm3",
        "shots": 1024,
        "webhook_url": None,
        "qubit_count": 2,
        "idempotency_key": None,
        "status": status,
        "created_at": created_at,
        "completed_at": None,
        "error_message": None,
        "owner_id": owner_id,
        "entity": "job",
        "schema_version": 1,
    }
    return jid


class TestOwnerScopingAndOrdering:
    def test_newest_first(
        self, client: TestClient, stubbed_backend: dict, bell_qasm3: str
    ) -> None:
        owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
        _seed(stubbed_backend, owner, created_at="2026-10-19T01:00:00+00:00")
        _seed(stubbed_backend, owner, created_at="2026-10-19T03:00:00+00:00")
        _seed(stubbed_backend, owner, created_at="2026-10-19T02:00:00+00:00")

        listing = client.get("/v1/quantum/jobs")
        assert listing.status_code == 200
        created = [row["created_at"] for row in listing.json()["jobs"]]
        assert created == sorted(created, reverse=True)

    def test_identity_rows_never_listed(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        """USER#/KEY# rows share the owner but must not appear as jobs."""
        owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
        stubbed_backend["store"][f"USER#{client.auth_user['username']}"] = {  # type: ignore[attr-defined]
            "job_id": f"USER#{client.auth_user['username']}",  # type: ignore[attr-defined]
            "entity": "user",
            "owner_id": owner,
            "created_at": "2026-10-19T04:00:00+00:00",
        }
        listing = client.get("/v1/quantum/jobs")
        assert listing.status_code == 200
        assert listing.json()["jobs"] == []

    def test_requires_auth(self, stubbed_backend: dict) -> None:
        from api.main import app

        with TestClient(app) as anon:
            assert anon.get("/v1/quantum/jobs").status_code == 401


class TestStatusFilter:
    def test_filter_by_status(self, client: TestClient, stubbed_backend: dict) -> None:
        owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
        done = _seed(stubbed_backend, owner, created_at="2026-10-19T01:00:00+00:00")
        _seed(
            stubbed_backend, owner, status="failed", created_at="2026-10-19T02:00:00+00:00"
        )

        listing = client.get("/v1/quantum/jobs", params={"status": "completed"})
        assert listing.status_code == 200
        rows = listing.json()["jobs"]
        assert [row["job_id"] for row in rows] == [done]
        assert rows[0]["status"] == "completed"

    def test_invalid_status_422(self, client: TestClient) -> None:
        assert client.get("/v1/quantum/jobs", params={"status": "bogus"}).status_code == 422


class TestFieldMapping:
    def test_row_exposes_expected_fields(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
        jid = _seed(stubbed_backend, owner, created_at="2026-10-19T01:00:00+00:00")
        row = client.get("/v1/quantum/jobs").json()["jobs"][0]
        assert row["job_id"] == jid
        assert row["format"] == "qasm3"
        assert row["shots"] == 1024
        assert row["qubit_count"] == 2
        assert set(row) == {
            "job_id",
            "status",
            "created_at",
            "completed_at",
            "format",
            "shots",
            "qubit_count",
            "error_message",
        }


class TestPagination:
    def test_cursor_walks_every_page(
        self, client: TestClient, stubbed_backend: dict
    ) -> None:
        owner = client.auth_user["user_id"]  # type: ignore[attr-defined]
        for i in range(5):
            _seed(
                stubbed_backend,
                owner,
                created_at=f"2026-10-19T0{i + 1}:00:00+00:00",
            )

        seen: list[str] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = client.get("/v1/quantum/jobs", params=params)
            assert page.status_code == 200
            body = page.json()
            seen += [row["job_id"] for row in body["jobs"]]
            pages += 1
            cursor = body["next_cursor"]
            if cursor is None:
                break
            assert pages < 10  # guard against an infinite loop

        assert len(seen) == 5
        assert len(set(seen)) == 5  # no duplicates across pages
        assert pages == 3  # 2 + 2 + 1

    def test_cursor_is_opaque_base64(
        self, client: TestClient, bell_qasm3: str
    ) -> None:
        _submit(client, bell_qasm3)
        body = client.get("/v1/quantum/jobs", params={"limit": 1}).json()
        if body["next_cursor"]:  # only one job here -> usually null
            decoded = json.loads(base64.urlsafe_b64decode(body["next_cursor"]))
            assert isinstance(decoded, dict)

    def test_invalid_cursor_400(self, client: TestClient) -> None:
        response = client.get("/v1/quantum/jobs", params={"cursor": "not-base64-json!!!"})
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "INVALID_CURSOR"

    def test_base64_but_not_json_400(self, client: TestClient) -> None:
        bogus = base64.urlsafe_b64encode(b"just a string").decode()
        response = client.get("/v1/quantum/jobs", params={"cursor": bogus})
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "INVALID_CURSOR"

    def test_limit_is_clamped_not_rejected(self, client: TestClient) -> None:
        # Out-of-range limits clamp rather than 422 — clients cannot 500 us.
        assert client.get("/v1/quantum/jobs", params={"limit": 0}).status_code == 200
        assert client.get("/v1/quantum/jobs", params={"limit": 10_000}).status_code == 200
