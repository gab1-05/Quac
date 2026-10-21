"""Shared fixtures for the Quac test suite."""

from __future__ import annotations

import pathlib
import sys
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

# Make the repository root importable so tests can use ``api.*`` / ``worker.*``.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------------------
# Canonical circuit fixtures
# --------------------------------------------------------------------------------------

BELL_QASM3: str = (
    "OPENQASM 3.0;\n"
    'include "stdgates.inc";\n'
    "qubit[2] q;\n"
    "bit[2] c;\n"
    "h q[0];\n"
    "cx q[0], q[1];\n"
    "c = measure q;\n"
)

GHZ5_QASM3: str = (
    "OPENQASM 3.0;\n"
    'include "stdgates.inc";\n'
    "qubit[5] q;\n"
    "bit[5] c;\n"
    "h q[0];\n"
    + "".join(f"cx q[{i}], q[{i + 1}];\n" for i in range(4))
    + "c = measure q;\n"
)

QASM3_29_QUBITS: str = (
    "OPENQASM 3.0;\n"
    'include "stdgates.inc";\n'
    "qubit[29] q;\n"
    "bit[29] c;\n"
    "h q[0];\n"
    "c = measure q;\n"
)

#: Angle-bracket include — classic file-exfiltration vector.
MALICIOUS_INCLUDE: str = (
    "OPENQASM 3.0;\n"
    "include </etc/passwd>;\n"
    "qubit[1] q;\n"
    "h q[0];\n"
)

#: Unbounded control flow — infinite-loop denial-of-service vector.
MALICIOUS_LOOP: str = (
    "OPENQASM 3.0;\n"
    "qubit[1] q;\n"
    "while (true) { h q[0]; }\n"
)

#: Custom definition block — runtime expansion / parser-DoS vector.
MALICIOUS_DEF: str = (
    "OPENQASM 3.0;\n"
    "gate evil a { h a; h a; h a; }\n"
    "qubit[1] q;\n"
    "evil q[0];\n"
)

#: Instruction smuggled inside a comment (must not execute, must not trip).
COMMENT_SMUGGLE: str = (
    "OPENQASM 3.0;\n"
    'include "stdgates.inc";\n'
    "// include </etc/passwd>; while(true){}\n"
    "qubit[1] q;\n"
    "h q[0];\n"
)


@pytest.fixture
def bell_qasm3() -> str:
    """Two-qubit Bell-state circuit in OpenQASM 3.0."""
    return BELL_QASM3


@pytest.fixture
def ghz5_qasm3() -> str:
    """Five-qubit GHZ circuit in OpenQASM 3.0."""
    return GHZ5_QASM3


@pytest.fixture
def generic_qiskit_json() -> dict[str, Any]:
    """Bell state in the generic Quac qiskit_json interchange format."""
    return {
        "num_qubits": 2,
        "num_clbits": 2,
        "instructions": [
            {"name": "h", "qubits": [0]},
            {"name": "cx", "qubits": [0, 1]},
            {"name": "measure", "qubits": [0], "clbits": [0]},
            {"name": "measure", "qubits": [1], "clbits": [1]},
        ],
    }


# --------------------------------------------------------------------------------------
# FastAPI test client with AWS side effects stubbed out
# --------------------------------------------------------------------------------------


@pytest.fixture
def identity_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """In-memory stand-in for the DynamoDB identity entities (P1).

    Mirrors the conditional semantics of :mod:`api.identity` — duplicate
    usernames raise, the request quota is a real counter with a real cap,
    revoked keys and expired sessions fail closed — so endpoint tests
    exercise the same code paths as production without touching AWS.
    """
    from api import auth as auth_module
    from api import identity as identity_module

    users: dict[str, dict[str, Any]] = {}
    keys: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    usage: dict[tuple[str, str], dict[str, int]] = {}
    audits: list[dict[str, Any]] = []
    clock = {"seq": 0}

    def _stamp() -> str:
        """Monotonically increasing created_at so GSI ordering is testable."""
        clock["seq"] += 1
        base = datetime(2026, 10, 19, tzinfo=UTC)
        return (base + timedelta(microseconds=clock["seq"])).isoformat()

    async def fake_create_user(
        username: str, password_hash: str, plan: str
    ) -> dict[str, Any]:
        if username in users:
            raise identity_module.UsernameTakenError(username)
        record = {
            "job_id": f"USER#{username}",
            "entity": "user",
            "user_id": str(uuid.uuid4()),
            "username": username,
            "password_hash": password_hash,
            "plan": plan,
            "created_at": _stamp(),
        }
        record["owner_id"] = record["user_id"]
        users[username] = record
        return dict(record)

    async def fake_get_user(username: str) -> dict[str, Any] | None:
        record = users.get(username)
        return dict(record) if record else None

    async def fake_create_session(
        token_hash: str, user_id: str, username: str, ttl_seconds: int
    ) -> dict[str, Any]:
        record = {
            "job_id": f"SESSION#{token_hash}",
            "entity": "session",
            "user_id": user_id,
            "username": username,
            "expires_at": int(time.time()) + ttl_seconds,
        }
        sessions[token_hash] = record
        return dict(record)

    async def fake_get_session(token_hash: str) -> dict[str, Any] | None:
        record = sessions.get(token_hash)
        if record is None:
            return None
        if int(record.get("expires_at", 0)) <= time.time():
            sessions.pop(token_hash, None)  # lazy TTL, like the real store
            return None
        return dict(record)

    async def fake_delete_session(token_hash: str) -> None:
        sessions.pop(token_hash, None)

    async def fake_create_api_key(
        key_id: str,
        user_id: str,
        username: str,
        secret_hash: str,
        prefix: str,
        label: str,
    ) -> dict[str, Any]:
        record = {
            "job_id": f"KEY#{key_id}",
            "entity": "api_key",
            "key_id": key_id,
            "secret_hash": secret_hash,
            "prefix": prefix,
            "label": label,
            "owner_id": user_id,
            "owner_username": username,
            "created_at": _stamp(),
            "revoked": False,
        }
        keys[key_id] = record
        return dict(record)

    async def fake_get_api_key(key_id: str) -> dict[str, Any] | None:
        record = keys.get(key_id)
        return dict(record) if record else None

    async def fake_list_api_keys(user_id: str) -> list[dict[str, Any]]:
        rows = [dict(r) for r in keys.values() if r.get("owner_id") == user_id]
        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        return rows

    async def fake_revoke_api_key(user_id: str, key_id: str) -> bool:
        record = keys.get(key_id)
        if record is None or record.get("owner_id") != user_id:
            return False
        record["revoked"] = True
        return True

    async def fake_get_usage(user_id: str, month: str) -> dict[str, Any] | None:
        counters = usage.get((user_id, month))
        return dict(counters) if counters else None

    async def fake_reserve_request_quota(
        user_id: str, month: str, max_requests: int
    ) -> int:
        counters = usage.setdefault((user_id, month), {"requests": 0, "compute_ms": 0})
        if counters["requests"] >= max_requests:
            raise identity_module.QuotaExceededError(month, max_requests)
        counters["requests"] += 1
        return counters["requests"]

    async def fake_record_audit(
        user_id: str, event: str, detail: dict[str, Any]
    ) -> None:
        audits.append({"user_id": user_id, "event": event, "detail": detail})

    monkeypatch.setattr(identity_module, "create_user", fake_create_user)
    monkeypatch.setattr(identity_module, "get_user", fake_get_user)
    monkeypatch.setattr(identity_module, "create_session", fake_create_session)
    monkeypatch.setattr(identity_module, "get_session", fake_get_session)
    monkeypatch.setattr(identity_module, "delete_session", fake_delete_session)
    monkeypatch.setattr(identity_module, "create_api_key", fake_create_api_key)
    monkeypatch.setattr(identity_module, "get_api_key", fake_get_api_key)
    monkeypatch.setattr(identity_module, "list_api_keys", fake_list_api_keys)
    monkeypatch.setattr(identity_module, "revoke_api_key", fake_revoke_api_key)
    monkeypatch.setattr(identity_module, "get_usage", fake_get_usage)
    monkeypatch.setattr(identity_module, "reserve_request_quota", fake_reserve_request_quota)
    monkeypatch.setattr(identity_module, "record_audit", fake_record_audit)

    def add_user(
        username: str, *, password_hash: str = "argon2id-fake", plan: str = "enterprise"
    ) -> dict[str, Any]:
        """Synchronous test helper: insert a user row directly."""
        if username in users:
            return dict(users[username])
        record = {
            "job_id": f"USER#{username}",
            "entity": "user",
            "user_id": str(uuid.uuid4()),
            "username": username,
            "password_hash": password_hash,
            "plan": plan,
            "created_at": _stamp(),
        }
        record["owner_id"] = record["user_id"]
        users[username] = record
        return dict(record)

    def add_key(user: dict[str, Any], *, label: str = "test") -> str:
        """Synchronous test helper: mint a real API key for ``user``."""
        key_id, secret, full_token, prefix = auth_module.mint_api_key()
        keys[key_id] = {
            "job_id": f"KEY#{key_id}",
            "entity": "api_key",
            "key_id": key_id,
            "secret_hash": auth_module.hash_api_secret(secret),
            "prefix": prefix,
            "label": label,
            "owner_id": user["user_id"],
            "owner_username": user["username"],
            "created_at": _stamp(),
            "revoked": False,
        }
        return full_token

    return {
        "users": users,
        "keys": keys,
        "sessions": sessions,
        "usage": usage,
        "audits": audits,
        "add_user": add_user,
        "add_key": add_key,
        "stamp": _stamp,
    }


@pytest.fixture
def stubbed_backend(
    monkeypatch: pytest.MonkeyPatch, identity_store: dict[str, Any]
) -> dict[str, Any]:
    """Replace DynamoDB and SQS adapters with in-memory async fakes.

    Records every interaction so endpoint tests can assert on persistence and
    enqueue ordering without touching AWS. Job-row fakes carry the P1
    multi-tenant attributes (``owner_id``, ``entity``, ``schema_version``) and
    the GSI list/count helpers operate on the same in-memory store.
    """
    from api import database, queue_service
    from api.schemas import JobStatus

    store: dict[str, dict[str, Any]] = {}
    calls: dict[str, list[Any]] = {
        "create": [],
        "update": [],
        "enqueue": [],
        "cancel": [],
        "list": [],
        "count_active": [],
    }
    seq = {"n": 0}

    def _stamp() -> str:
        seq["n"] += 1
        base = datetime(2026, 10, 19, tzinfo=UTC)
        return (base + timedelta(microseconds=seq["n"])).isoformat()

    async def fake_create(
        job_id: str,
        payload_json: str,
        *,
        circuit_format: Any,
        shots: int,
        webhook_url: str | None,
        qubit_count: int,
        idempotency_key: str | None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "job_id": job_id,
            "payload": payload_json,
            "format": circuit_format.value,
            "shots": shots,
            "webhook_url": webhook_url,
            "qubit_count": qubit_count,
            "idempotency_key": idempotency_key,
            "status": JobStatus.QUEUED.value,
            "created_at": _stamp(),
            "completed_at": None,
            "error_message": None,
            "owner_id": owner_id or "system",
            "entity": "job",
            "schema_version": 1,
        }
        store[job_id] = record
        calls["create"].append(job_id)
        return record

    async def fake_update(
        job_id: str,
        status: JobStatus,
        *,
        error_message: str | None = None,
        simulation_time_ms: int | None = None,
        result_s3_key: str | None = None,
        unless_cancelled: bool = False,
    ) -> dict[str, Any]:
        del unless_cancelled  # guarded transitions are exercised against DynamoDB fakes
        record = store[job_id]
        record["status"] = status.value
        record["error_message"] = error_message
        calls["update"].append((job_id, status.value))
        return record

    async def fake_cancel(job_id: str) -> dict[str, Any]:
        if job_id not in store:
            raise database.JobRecordNotFoundError(job_id)
        record = store[job_id]
        if record["status"] not in (JobStatus.QUEUED.value, JobStatus.PROCESSING.value):
            raise database.JobNotCancellableError(job_id, str(record["status"]))
        record["status"] = JobStatus.CANCELLED.value
        record["completed_at"] = "2026-10-19T00:00:00+00:00"
        calls["cancel"].append(job_id)
        return record

    async def fake_get(job_id: str) -> dict[str, Any]:
        if job_id not in store:
            raise database.JobRecordNotFoundError(job_id)
        return store[job_id]

    async def fake_enqueue(job_id: str, request: Any, qubit_count: int) -> str:
        calls["enqueue"].append((job_id, qubit_count))
        return f"msg-{job_id}"

    async def fake_list_by_owner(
        owner_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
        exclusive_start_key: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        calls["list"].append(owner_id)
        rows = [
            dict(r)
            for r in store.values()
            if r.get("owner_id") == owner_id and r.get("entity", "job") == "job"
        ]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        start = int((exclusive_start_key or {}).get("offset", 0))
        page = rows[start : start + limit]
        last_key = {"offset": start + limit} if start + limit < len(rows) else None
        return page, last_key

    async def fake_count_active(owner_id: str) -> int:
        calls["count_active"].append(owner_id)
        return sum(
            1
            for r in store.values()
            if r.get("owner_id") == owner_id
            and r.get("status") in (JobStatus.QUEUED.value, JobStatus.PROCESSING.value)
        )

    monkeypatch.setattr(database, "create_job_record", fake_create)
    monkeypatch.setattr(database, "update_job_status", fake_update)
    monkeypatch.setattr(database, "get_job_record", fake_get)
    monkeypatch.setattr(database, "cancel_job_record", fake_cancel)
    monkeypatch.setattr(database, "list_jobs_by_owner", fake_list_by_owner)
    monkeypatch.setattr(database, "count_active_jobs", fake_count_active)
    monkeypatch.setattr(queue_service, "enqueue_simulation", fake_enqueue)

    return {"store": store, "calls": calls, "identity": identity_store}


@pytest.fixture
def client(
    stubbed_backend: dict[str, Any], identity_store: dict[str, Any]
) -> Iterator[Any]:
    """FastAPI TestClient with lifespan active, AWS stubbed and *authenticated*.

    Each test gets a fresh enterprise-plan user and a real API key, so no
    test trips another test's rate-limit or quota state and every /v1 route
    is reachable without per-test auth boilerplate. The raw TestClient is
    yielded; ``client.auth_user`` / ``client.auth_key`` expose the identity.
    """
    from fastapi.testclient import TestClient

    from api.main import app, rate_limiter

    rate_limiter._max_requests = 1000  # tests must not trip the limiter by default

    user = identity_store["add_user"](
        f"tester-{uuid.uuid4().hex[:8]}", plan="enterprise"
    )
    api_key = identity_store["add_key"](user)

    with TestClient(app, headers={"X-API-Key": api_key}) as test_client:
        test_client.auth_user = user  # type: ignore[attr-defined]
        test_client.auth_key = api_key  # type: ignore[attr-defined]
        yield test_client
