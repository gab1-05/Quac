"""API-key management tests (P1): mint, list, revoke, key-based auth.

Covers the show-once secret contract, cross-tenant revoke 404, and revoked
keys failing closed with the generic 401.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def _create_key(client: TestClient, label: str = "default") -> dict:
    response = client.post("/v1/keys", json={"label": label})
    assert response.status_code == 201, response.text
    return response.json()


class TestCreateKey:
    def test_create_returns_secret_exactly_once(self, client: TestClient) -> None:
        body = _create_key(client, label="ci-runner")
        assert body["key"].startswith(f"qk_{body['key_id']}_")
        assert body["prefix"] == f"qk_{body['key_id']}"
        assert body["label"] == "ci-runner"
        assert body["created_at"]

        # The secret must never appear again in listings.
        listing = client.get("/v1/keys").json()
        assert body["key"] not in str(listing)
        assert all("key" not in row for row in listing["keys"])

    def test_created_key_authenticates(self, client: TestClient) -> None:
        created = _create_key(client)
        from api.main import app

        with TestClient(app, headers={"X-API-Key": created["key"]}) as fresh:
            me = fresh.get("/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["username"] == client.auth_user["username"]  # type: ignore[attr-defined]

    def test_label_is_required_shape(self, client: TestClient) -> None:
        assert client.post("/v1/keys", json={"label": ""}).status_code == 422
        assert client.post("/v1/keys", json={}).status_code == 201  # default label


class TestListKeys:
    def test_list_shows_metadata_never_secrets(self, client: TestClient) -> None:
        created = _create_key(client, label="one")
        listing = client.get("/v1/keys")
        assert listing.status_code == 200
        keys = listing.json()["keys"]
        row = next(k for k in keys if k["key_id"] == created["key_id"])
        assert row["label"] == "one"
        assert row["revoked"] is False
        assert row["prefix"] == f"qk_{created['key_id']}"
        assert "secret_hash" not in row
        assert "key" not in row

    def test_requires_authentication(self, stubbed_backend: dict) -> None:
        from api.main import app

        with TestClient(app) as anon:
            assert anon.get("/v1/keys").status_code == 401


class TestRevokeKey:
    def test_revoke_then_key_fails_401(self, client: TestClient) -> None:
        created = _create_key(client)
        revoked = client.delete(f"/v1/keys/{created['key_id']}")
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] is True

        from api.main import app

        with TestClient(app, headers={"X-API-Key": created["key"]}) as fresh:
            assert fresh.get("/v1/auth/me").status_code == 401

    def test_revoke_is_listed(self, client: TestClient) -> None:
        created = _create_key(client)
        client.delete(f"/v1/keys/{created['key_id']}")
        listing = client.get("/v1/keys").json()["keys"]
        assert listing[0]["revoked"] is True

    def test_unknown_key_404(self, client: TestClient) -> None:
        response = client.delete(f"/v1/keys/{uuid.uuid4().hex}")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "KEY_NOT_FOUND"

    def test_cross_tenant_revoke_404_and_key_still_valid(
        self, client: TestClient, identity_store: dict
    ) -> None:
        created = _create_key(client)
        other_user = identity_store["add_user"](f"other-{uuid.uuid4().hex[:8]}")
        other_key = identity_store["add_key"](other_user)

        from api.main import app

        with TestClient(app, headers={"X-API-Key": other_key}) as other:
            response = other.delete(f"/v1/keys/{created['key_id']}")
            assert response.status_code == 404  # no existence oracle across tenants
            # The victim's key still authenticates.
            listing = other.get("/v1/keys")
            assert listing.status_code == 200
            seen = [k["key_id"] for k in listing.json()["keys"]]
            assert created["key_id"] not in seen  # sees only its own keys

        # Victim's key untouched.
        with TestClient(app, headers={"X-API-Key": created["key"]}) as victim:
            assert victim.get("/v1/auth/me").status_code == 200


class TestKeyAuthenticatesApiRoutes:
    def test_api_key_can_submit(self, client: TestClient, bell_qasm3: str) -> None:
        response = client.post(
            "/v1/quantum/simulate",
            json={"circuit_data": bell_qasm3, "format": "qasm3"},
        )
        assert response.status_code == 202

    def test_revoked_key_cannot_submit(self, client: TestClient, bell_qasm3: str) -> None:
        created = _create_key(client)
        client.delete(f"/v1/keys/{created['key_id']}")
        from api.main import app

        with TestClient(app, headers={"X-API-Key": created["key"]}) as fresh:
            response = fresh.post(
                "/v1/quantum/simulate",
                json={"circuit_data": bell_qasm3, "format": "qasm3"},
            )
        assert response.status_code == 401
        assert response.json()["detail"]["error"] == "UNAUTHENTICATED"
