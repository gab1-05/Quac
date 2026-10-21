"""Authentication tests (P1): registration, login, sessions, /me.

Covers the generic-401 contract (no username oracle), cookie handling,
username normalisation and the password policy.
"""

from __future__ import annotations

import re

import pytest
from conftest import BELL_QASM3
from fastapi.testclient import TestClient

from api.auth import (
    SESSION_COOKIE_NAME,
    hash_password,
    mint_api_key,
    mint_session_token,
    parse_api_key,
    session_token_hash,
    verify_password,
)

STRONG_PASSWORD = "correct-horse-battery-staple"


def _anon_client(stubbed_backend: dict) -> TestClient:
    """A TestClient with no credentials attached."""
    from api.main import app

    return TestClient(app)


class TestPasswordHashing:
    def test_hash_round_trips(self) -> None:
        digest = hash_password(STRONG_PASSWORD)
        assert verify_password(digest, STRONG_PASSWORD)

    def test_wrong_password_fails(self) -> None:
        digest = hash_password(STRONG_PASSWORD)
        assert not verify_password(digest, "not-the-password")

    def test_hashes_are_salted(self) -> None:
        assert hash_password(STRONG_PASSWORD) != hash_password(STRONG_PASSWORD)

    def test_malformed_hash_fails_closed(self) -> None:
        assert not verify_password("not-a-real-hash", STRONG_PASSWORD)


class TestApiKeyPrimitives:
    def test_mint_and_parse_round_trip(self) -> None:
        key_id, secret, token, prefix = mint_api_key()
        assert token.startswith(f"qk_{key_id}_")
        assert prefix == f"qk_{key_id}"
        assert parse_api_key(token) == (key_id, secret)

    @pytest.mark.parametrize(
        "raw",
        ["", "garbage", "qk_onlytwo", "sk_abc_def", "qk__secret", "qk_keyid_"],
    )
    def test_malformed_tokens_rejected(self, raw: str) -> None:
        assert parse_api_key(raw) is None

    def test_session_token_hash_is_sha256(self) -> None:
        raw, token_hash = mint_session_token()
        assert token_hash == session_token_hash(raw)
        assert len(token_hash) == 64
        # The raw token must never equal the stored hash.
        assert raw != token_hash


class TestRegistration:
    def test_register_returns_201_and_sets_cookie(
        self, stubbed_backend: dict, identity_store: dict
    ) -> None:
        with _anon_client(stubbed_backend) as client:
            response = client.post(
                "/v1/auth/register", json={"username": "alice", "password": STRONG_PASSWORD}
            )
        assert response.status_code == 201
        body = response.json()
        assert body["username"] == "alice"
        assert body["plan"] == "free"  # open registration lands on the free tier
        assert body["limits"]["name"] == "free"
        assert SESSION_COOKIE_NAME in response.cookies
        assert "alice" in identity_store["users"]

    def test_username_is_normalised_to_lowercase(self, identity_store: dict) -> None:
        with _anon_client({}) as client:
            response = client.post(
                "/v1/auth/register", json={"username": "Alice", "password": STRONG_PASSWORD}
            )
        assert response.status_code == 201
        assert response.json()["username"] == "alice"
        assert "alice" in identity_store["users"]

    def test_duplicate_username_conflicts(self, identity_store: dict) -> None:
        identity_store["add_user"]("alice")
        with _anon_client({}) as client:
            response = client.post(
                "/v1/auth/register", json={"username": "alice", "password": STRONG_PASSWORD}
            )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "USERNAME_TAKEN"

    @pytest.mark.parametrize(
        "username", ["ab", "x" * 33, "-leading", "_leading", "has space", "has.dot"]
    )
    def test_invalid_usernames_rejected(self, username: str) -> None:
        with _anon_client({}) as client:
            response = client.post(
                "/v1/auth/register", json={"username": username, "password": STRONG_PASSWORD}
            )
        assert response.status_code == 422

    @pytest.mark.parametrize("password", ["short1234", "a" * 129])
    def test_password_policy_enforced(self, password: str) -> None:
        with _anon_client({}) as client:
            response = client.post(
                "/v1/auth/register", json={"username": "bob", "password": password}
            )
        assert response.status_code == 422

    def test_username_pattern_contract(self) -> None:
        # The schema pattern and this test must agree on what is legal.
        pattern = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
        assert pattern.match("tester-01")
        assert not pattern.match("Tester-01")  # uppercase is normalised first


class TestLogin:
    def test_login_success_sets_cookie(self, identity_store: dict) -> None:
        digest = hash_password(STRONG_PASSWORD)
        identity_store["add_user"]("carol", password_hash=digest)
        with _anon_client({}) as client:
            response = client.post(
                "/v1/auth/login", json={"username": "carol", "password": STRONG_PASSWORD}
            )
        assert response.status_code == 200
        assert response.json()["username"] == "carol"
        assert SESSION_COOKIE_NAME in response.cookies

    def test_unknown_user_and_wrong_password_are_indistinguishable(
        self, identity_store: dict
    ) -> None:
        digest = hash_password(STRONG_PASSWORD)
        identity_store["add_user"]("dave", password_hash=digest)
        with _anon_client({}) as client:
            unknown = client.post(
                "/v1/auth/login", json={"username": "nobody", "password": STRONG_PASSWORD}
            )
            wrong = client.post(
                "/v1/auth/login", json={"username": "dave", "password": "wrong-password-x"}
            )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()  # identical envelope, no oracle

    def test_session_cookie_authenticates(self, identity_store: dict) -> None:
        digest = hash_password(STRONG_PASSWORD)
        user = identity_store["add_user"]("erin", password_hash=digest)
        raw_token, token_hash = mint_session_token()
        # Insert through the fake store's session row (mirrors create_session).
        identity_store["sessions"][token_hash] = {
            "job_id": f"SESSION#{token_hash}",
            "entity": "session",
            "user_id": user["user_id"],
            "username": "erin",
            "expires_at": 9_999_999_999,
        }
        with _anon_client({}) as client:
            client.cookies.set(SESSION_COOKIE_NAME, raw_token)
            response = client.get("/v1/auth/me")
        assert response.status_code == 200
        assert response.json()["username"] == "erin"

    def test_expired_session_reads_as_unauthenticated(self, identity_store: dict) -> None:
        user = identity_store["add_user"]("flo")
        raw_token, token_hash = mint_session_token()
        identity_store["sessions"][token_hash] = {
            "job_id": f"SESSION#{token_hash}",
            "entity": "session",
            "user_id": user["user_id"],
            "username": "flo",
            "expires_at": 1,  # long past
        }
        with _anon_client({}) as client:
            client.cookies.set(SESSION_COOKIE_NAME, raw_token)
            assert client.get("/v1/auth/me").status_code == 401
            # The expired row must not authenticate a retry either.
            assert client.get("/v1/auth/me").status_code == 401


class TestLogout:
    def test_logout_deletes_session_and_clears_cookie(self, identity_store: dict) -> None:
        digest = hash_password(STRONG_PASSWORD)
        user = identity_store["add_user"]("frank", password_hash=digest)
        raw_token, token_hash = mint_session_token()
        identity_store["sessions"][token_hash] = {
            "job_id": f"SESSION#{token_hash}",
            "entity": "session",
            "user_id": user["user_id"],
            "username": "frank",
            "expires_at": 9_999_999_999,
        }
        with _anon_client({}) as client:
            client.cookies.set(SESSION_COOKIE_NAME, raw_token)
            assert client.get("/v1/auth/me").status_code == 200
            response = client.post("/v1/auth/logout")
            assert response.status_code == 200
            # The session row is gone; /me now fails.
            assert client.get("/v1/auth/me").status_code == 401
        assert token_hash not in identity_store["sessions"]


class TestMe:
    def test_me_returns_identity_plan_and_usage(
        self, client: TestClient, identity_store: dict
    ) -> None:
        response = client.get("/v1/auth/me")
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == client.auth_user["username"]  # type: ignore[attr-defined]
        assert body["plan"] == "enterprise"
        assert body["limits"]["monthly_requests"] == 200_000
        assert body["usage"]["month"]
        assert body["usage"]["requests"] == 0

    def test_me_requires_authentication(self, stubbed_backend: dict) -> None:
        with _anon_client(stubbed_backend) as client:
            assert client.get("/v1/auth/me").status_code == 401

    def test_me_counts_requests_after_submit(
        self, client: TestClient, bell_qasm3: str
    ) -> None:
        client.post(
            "/v1/quantum/simulate", json={"circuit_data": bell_qasm3, "format": "qasm3"}
        )
        usage = client.get("/v1/auth/me").json()["usage"]
        assert usage["requests"] == 1


class TestUnauthenticatedSurface:
    """Every /v1/quantum route except /formats rejects missing credentials."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/v1/quantum/simulate"),
            ("POST", "/v1/quantum/analyze"),
            ("GET", "/v1/quantum/jobs"),
        ],
    )
    def test_missing_credentials_rejected(
        self, stubbed_backend: dict, method: str, path: str
    ) -> None:
        with _anon_client(stubbed_backend) as client:
            response = client.request(method, path, json={})
        assert response.status_code == 401
        assert response.json()["detail"]["error"] == "UNAUTHENTICATED"

    def test_job_routes_reject_missing_credentials(self, stubbed_backend: dict) -> None:
        import uuid

        job_id = str(uuid.uuid4())
        with _anon_client(stubbed_backend) as client:
            for path in (f"/v1/quantum/jobs/{job_id}", f"/v1/quantum/jobs/{job_id}/result"):
                assert client.get(path).status_code == 401, path
            assert client.post(f"/v1/quantum/jobs/{job_id}/cancel").status_code == 401

    def test_invalid_key_rejected(self, stubbed_backend: dict) -> None:
        with _anon_client(stubbed_backend) as client:
            response = client.get(
                "/v1/auth/me", headers={"X-API-Key": "qk_deadbeefdeadbeef_notarealsecret"}
            )
        assert response.status_code == 401

    def test_formats_and_healthz_stay_open(self, stubbed_backend: dict) -> None:
        with _anon_client(stubbed_backend) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/v1/quantum/formats").status_code == 200

    def test_session_cookie_with_bogus_token_rejected(self, stubbed_backend: dict) -> None:
        with _anon_client(stubbed_backend) as client:
            client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-token")
            assert client.get("/v1/auth/me").status_code == 401


class TestCircuitStillFires:
    def test_authenticated_submit_still_works(self, client: TestClient) -> None:
        """Sanity: the auth layer did not break submission."""
        response = client.post(
            "/v1/quantum/simulate", json={"circuit_data": BELL_QASM3, "format": "qasm3"}
        )
        assert response.status_code == 202
