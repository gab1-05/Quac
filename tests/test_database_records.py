"""DynamoDB record-shape and guarded-transition tests.

``DynamoJobStore`` is exercised against an in-memory fake table that emulates
the two condition expressions the API relies on (conditional insert and the
non-terminal cancel guard), so the real store logic — item shape, owner
stamping, condition construction, and error mapping — is verified without
AWS.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from api.database import (
    DEFAULT_OWNER_ID,
    RECORD_SCHEMA_VERSION,
    DynamoJobStore,
    JobNotCancellableError,
    JobRecordNotFoundError,
)
from api.schemas import CircuitFormat, JobStatus

TERMINAL_TRIGGER = "ConditionalCheckFailedException"


def _client_error(code: str, operation: str = "UpdateItem") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "The conditional request failed"}},
        operation,
    )


class FakeTable:
    """Minimal DynamoDB table double covering the store's call patterns."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def put_item(self, *, Item: dict[str, Any], ConditionExpression: str) -> dict[str, Any]:
        self.put_calls.append({"Item": Item, "ConditionExpression": ConditionExpression})
        job_id = Item["job_id"]
        if "attribute_not_exists(job_id)" in ConditionExpression and job_id in self.items:
            raise _client_error(TERMINAL_TRIGGER, "PutItem")
        self.items[job_id] = dict(Item)
        return {}

    def get_item(self, *, Key: dict[str, Any]) -> dict[str, Any]:
        item = self.items.get(str(Key["job_id"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        job_id = str(kwargs["Key"]["job_id"])
        condition = kwargs.get("ConditionExpression")
        current = self.items.get(job_id)
        values = kwargs.get("ExpressionAttributeValues", {})

        if condition is not None:
            if "#status = :queued" in condition:
                # API cancel guard: item must exist and be non-terminal.
                if current is None or current.get("status") not in {
                    values.get(":queued"),
                    values.get(":processing"),
                }:
                    raise _client_error(TERMINAL_TRIGGER)
            elif "#status <> :cancelled" in condition:
                # Worker guarded transition: refuses to overwrite a cancel.
                if current is None or current.get("status") == values.get(":cancelled"):
                    raise _client_error(TERMINAL_TRIGGER)
            elif "attribute_exists(job_id)" in condition and current is None:
                raise _client_error(TERMINAL_TRIGGER)

        # Apply the SET clause: placeholders only, so ", " splitting is safe.
        target = dict(current) if current else {"job_id": job_id}
        for assignment in kwargs.get("UpdateExpression", "").removeprefix("SET ").split(", "):
            lhs, rhs = assignment.split(" = ", 1)
            target[lhs.removeprefix("#")] = values.get(rhs)
        self.items[job_id] = target
        return {"Attributes": dict(target)}


@pytest.fixture
def store() -> DynamoJobStore:
    """A DynamoJobStore bound to a fresh fake table (no AWS initialisation)."""
    instance = DynamoJobStore(table_name="quac-jobs-test", region="us-east-1")
    instance._table = FakeTable()  # type: ignore[assignment]
    return instance


class TestRecordShape:
    def test_create_stamps_owner_and_schema_version(
        self, store: DynamoJobStore
    ) -> None:
        item = store.create_job(
            "job-1",
            "{}",
            circuit_format=CircuitFormat.QASM3,
            shots=1024,
            webhook_url=None,
            qubit_count=2,
            idempotency_key=None,
        )
        assert item["owner_id"] == DEFAULT_OWNER_ID
        assert item["schema_version"] == RECORD_SCHEMA_VERSION
        assert item["status"] == JobStatus.QUEUED.value
        assert item["webhook_url"] is None
        assert item["created_at"]  # ISO timestamp for the owner GSI range key

    def test_create_accepts_explicit_owner(self, store: DynamoJobStore) -> None:
        item = store.create_job(
            "job-2",
            "{}",
            circuit_format=CircuitFormat.QASM3,
            shots=16,
            webhook_url="https://consumer.example.com/hook",
            qubit_count=1,
            idempotency_key="tok",
            owner_id="user-42",
        )
        assert item["owner_id"] == "user-42"
        assert item["schema_version"] == RECORD_SCHEMA_VERSION

    def test_duplicate_job_id_raises_file_exists(self, store: DynamoJobStore) -> None:
        kwargs = {
            "payload_json": "{}",
            "circuit_format": CircuitFormat.QASM3,
            "shots": 16,
            "webhook_url": None,
            "qubit_count": 1,
            "idempotency_key": None,
        }
        store.create_job("job-dup", **kwargs)
        with pytest.raises(FileExistsError):
            store.create_job("job-dup", **kwargs)


class TestCancelTransition:
    def test_cancel_queued_job_updates_status(self, store: DynamoJobStore) -> None:
        store.create_job(
            "job-c",
            "{}",
            circuit_format=CircuitFormat.QASM3,
            shots=16,
            webhook_url=None,
            qubit_count=1,
            idempotency_key=None,
        )
        updated = store.cancel("job-c")
        assert updated["status"] == JobStatus.CANCELLED.value
        assert updated["completed_at"]

        update_call = store._table.update_calls[-1]
        condition = update_call["ConditionExpression"]
        assert "attribute_exists(job_id)" in condition
        assert ":queued" in condition and ":processing" in condition

    def test_cancel_unknown_job_raises_not_found(self, store: DynamoJobStore) -> None:
        with pytest.raises(JobRecordNotFoundError):
            store.cancel("job-missing")

    @pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
    def test_cancel_terminal_job_raises_conflict(
        self, store: DynamoJobStore, terminal: str
    ) -> None:
        store.create_job(
            "job-t",
            "{}",
            circuit_format=CircuitFormat.QASM3,
            shots=16,
            webhook_url=None,
            qubit_count=1,
            idempotency_key=None,
        )
        store._table.items["job-t"]["status"] = terminal
        with pytest.raises(JobNotCancellableError) as excinfo:
            store.cancel("job-t")
        assert excinfo.value.current_status == terminal
        # The terminal state must not be overwritten.
        assert store._table.items["job-t"]["status"] == terminal


class TestGuardedWorkerStyleUpdate:
    """``unless_cancelled`` transitions are condition-guarded in the store."""

    def _create(self, store: DynamoJobStore, job_id: str) -> None:
        store.create_job(
            job_id,
            "{}",
            circuit_format=CircuitFormat.QASM3,
            shots=16,
            webhook_url=None,
            qubit_count=1,
            idempotency_key=None,
        )

    def test_unguarded_update_has_no_cancel_guard(self, store: DynamoJobStore) -> None:
        self._create(store, "job-u")
        store.update_status("job-u", JobStatus.PROCESSING)
        condition = store._table.update_calls[-1].get("ConditionExpression", "")
        # Existence check only — no cancelled-status guard unless asked for.
        assert "attribute_exists(job_id)" in condition
        assert ":cancelled" not in condition
