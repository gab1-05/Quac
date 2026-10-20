"""Shared fixtures for the Quac test suite."""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Iterator
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
def stubbed_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace DynamoDB and SQS adapters with in-memory async fakes.

    Records every interaction so endpoint tests can assert on persistence and
    enqueue ordering without touching AWS.
    """
    from api import database, queue_service
    from api.schemas import JobStatus

    store: dict[str, dict[str, Any]] = {}
    calls: dict[str, list[Any]] = {"create": [], "update": [], "enqueue": []}

    async def fake_create(
        job_id: str,
        payload_json: str,
        *,
        circuit_format: Any,
        shots: int,
        webhook_url: str,
        qubit_count: int,
        idempotency_key: str | None,
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
            "created_at": "2026-10-19T00:00:00+00:00",
            "completed_at": None,
            "error_message": None,
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
    ) -> dict[str, Any]:
        record = store[job_id]
        record["status"] = status.value
        record["error_message"] = error_message
        calls["update"].append((job_id, status.value))
        return record

    async def fake_get(job_id: str) -> dict[str, Any]:
        if job_id not in store:
            raise database.JobRecordNotFoundError(job_id)
        return store[job_id]

    async def fake_enqueue(job_id: str, request: Any, qubit_count: int) -> str:
        calls["enqueue"].append((job_id, qubit_count))
        return f"msg-{job_id}"

    monkeypatch.setattr(database, "create_job_record", fake_create)
    monkeypatch.setattr(database, "update_job_status", fake_update)
    monkeypatch.setattr(database, "get_job_record", fake_get)
    monkeypatch.setattr(queue_service, "enqueue_simulation", fake_enqueue)

    return {"store": store, "calls": calls}


@pytest.fixture
def client(stubbed_backend: dict[str, Any]) -> Iterator[Any]:
    """FastAPI TestClient with lifespan active and AWS stubbed."""
    from fastapi.testclient import TestClient

    from api.main import app, rate_limiter

    rate_limiter._max_requests = 1000  # tests must not trip the limiter by default
    with TestClient(app) as test_client:
        yield test_client
