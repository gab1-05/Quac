"""Worker-side cancellation and duplicate-delivery guards.

Verifies the Phase-0 reliability rules:

* a job cancelled before execution starts is never simulated;
* a redelivered message for an already-terminal job is acknowledged without
  re-executing (no duplicate billing);
* a job cancelled while its result was being finalised discards the uploaded
  artifact and reports ``simulation.cancelled`` instead of ``completed``;
* guarded status transitions always carry ``unless_cancelled=True``.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")

from worker import worker as quac_worker


def _envelope(job_id: str, *, webhook_url: str | None = None) -> dict:
    return {
        "job_id": job_id,
        "circuit_data": "OPENQASM 3.0;\nqubit[1] q;\nh q[0];\n",
        "format": "qasm3",
        "shots": 16,
        "webhook_url": webhook_url,
    }


def _event(job_id: str, *, webhook_url: str | None = None) -> dict:
    envelope = json.dumps(_envelope(job_id, webhook_url=webhook_url))
    return {"Records": [{"messageId": f"m-{job_id}", "body": envelope}]}


@pytest.fixture
def dispatch_capture(monkeypatch: pytest.MonkeyPatch) -> list:
    captured: list = []
    monkeypatch.setattr(
        quac_worker,
        "_dispatch_cancelled_webhook",
        lambda job_id, url, elapsed: captured.append((job_id, url)),
    )
    return captured


@pytest.fixture
def update_capture(monkeypatch: pytest.MonkeyPatch) -> list:
    captured: list = []

    def capture(job_id: str, status: str, **kwargs) -> None:
        captured.append((job_id, status, kwargs.get("unless_cancelled")))

    monkeypatch.setattr(quac_worker, "_update_job_status", capture)
    return captured


@pytest.fixture
def no_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any attempt to reach the simulator fails the test loudly."""

    def explode(*args, **kwargs):
        raise AssertionError("simulator must not run for this job")

    monkeypatch.setattr(quac_worker, "run_simulation", explode)


class TestPreExecutionGuards:
    def test_cancelled_job_never_executes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dispatch_capture: list,
        update_capture: list,
        no_simulation: None,
    ) -> None:
        monkeypatch.setattr(
            quac_worker,
            "_read_job_record",
            lambda job_id: {
                "job_id": job_id,
                "status": "cancelled",
                "webhook_url": "https://consumer.example.com/hook",
            },
        )

        result = quac_worker.lambda_handler(_event("job-cancelled"), None)

        assert result == {"batchItemFailures": []}  # acknowledged, not retried
        assert update_capture == []  # no status transition attempted
        assert dispatch_capture == [
            ("job-cancelled", "https://consumer.example.com/hook")
        ]

    def test_cancelled_job_without_webhook_acknowledges_silently(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dispatch_capture: list,
        update_capture: list,
        no_simulation: None,
    ) -> None:
        monkeypatch.setattr(
            quac_worker,
            "_read_job_record",
            lambda job_id: {"job_id": job_id, "status": "cancelled", "webhook_url": None},
        )

        result = quac_worker.lambda_handler(_event("job-silent"), None)

        assert result == {"batchItemFailures": []}
        # The guarded path still resolves the (absent) webhook; the real helper
        # suppresses dispatch for falsy URLs, so the capture proves it received
        # None rather than attempting a network call.
        assert dispatch_capture == [("job-silent", None)]
        assert update_capture == []

    def test_duplicate_delivery_of_terminal_job_is_acked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dispatch_capture: list,
        update_capture: list,
        no_simulation: None,
    ) -> None:
        monkeypatch.setattr(
            quac_worker,
            "_read_job_record",
            lambda job_id: {"job_id": job_id, "status": "completed", "webhook_url": None},
        )

        result = quac_worker.lambda_handler(_event("job-dup"), None)

        assert result == {"batchItemFailures": []}
        assert update_capture == []  # nothing re-runs, nothing re-bills
        assert dispatch_capture == []

    @pytest.mark.parametrize("terminal", ["failed", "cancelled"])
    def test_terminal_statuses_short_circuit(
        self,
        terminal: str,
        monkeypatch: pytest.MonkeyPatch,
        update_capture: list,
        no_simulation: None,
    ) -> None:
        monkeypatch.setattr(
            quac_worker,
            "_read_job_record",
            lambda job_id: {"job_id": job_id, "status": terminal, "webhook_url": None},
        )
        result = quac_worker.lambda_handler(_event(f"job-{terminal}"), None)
        assert result == {"batchItemFailures": []}
        assert update_capture == []

    def test_missing_record_falls_through_to_execution(
        self,
        monkeypatch: pytest.MonkeyPatch,
        update_capture: list,
    ) -> None:
        """A transient read miss must not block a valid message."""
        monkeypatch.setattr(quac_worker, "_read_job_record", lambda job_id: None)
        ran: list[str] = []
        monkeypatch.setattr(
            quac_worker,
            "run_simulation",
            lambda *a, **k: ran.append("ran")
            or quac_worker.SimulationResult(
                counts={"00": 8, "11": 8},
                shots=16,
                method="statevector",
                num_qubits=2,
                depth=2,
            ),
        )
        monkeypatch.setattr(quac_worker, "_upload_result", lambda key, doc: None)
        monkeypatch.setattr(quac_worker, "_presign_download", lambda key: "https://signed")

        result = quac_worker.lambda_handler(
            _event("job-fallthrough", webhook_url=None), None
        )

        assert result == {"batchItemFailures": []}
        assert ran == ["ran"]
        # Processing and completion transitions must both be cancel-guarded.
        assert update_capture == [
            ("job-fallthrough", "processing", True),
            ("job-fallthrough", "completed", True),
        ]


class TestFinalizationRace:
    def test_cancel_during_finalization_discards_artifact(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dispatch_capture: list,
    ) -> None:
        webhook = "https://consumer.example.com/hook"
        monkeypatch.setattr(
            quac_worker,
            "_read_job_record",
            lambda job_id: {"job_id": job_id, "status": "processing", "webhook_url": webhook},
        )

        transitions: list[tuple[str, str, bool | None]] = []

        def guarded_update(job_id: str, status: str, **kwargs) -> None:
            unless_cancelled = kwargs.get("unless_cancelled")
            transitions.append((job_id, status, unless_cancelled))
            if status == "completed":
                raise quac_worker.JobCancelledError(f"job {job_id} was cancelled")

        uploads: list[str] = []
        deletions: list[str] = []
        monkeypatch.setattr(quac_worker, "_update_job_status", guarded_update)
        monkeypatch.setattr(
            quac_worker,
            "run_simulation",
            lambda *a, **k: quac_worker.SimulationResult(
                counts={"00": 8, "11": 8},
                shots=16,
                method="statevector",
                num_qubits=2,
                depth=2,
            ),
        )
        monkeypatch.setattr(
            quac_worker, "_upload_result", lambda key, doc: uploads.append(key)
        )
        monkeypatch.setattr(quac_worker, "_presign_download", lambda key: "https://signed")
        monkeypatch.setattr(quac_worker, "_delete_result_object", deletions.append)

        result = quac_worker.lambda_handler(
            _event("job-race", webhook_url=webhook), None
        )

        assert result == {"batchItemFailures": []}  # business outcome, acked
        assert len(uploads) == 1
        assert deletions == uploads  # the orphaned artifact was removed
        # Both transitions were guarded; completion was attempted and rejected.
        assert transitions[0] == ("job-race", "processing", True)
        assert transitions[-1] == ("job-race", "completed", True)
        assert dispatch_capture == [("job-race", webhook)]


class TestGuardedUpdateConstruction:
    def test_unless_cancelled_adds_condition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        class FakeTable:
            def update_item(self, **kwargs):
                captured.update(kwargs)
                return {}

        class FakeResource:
            def Table(self, name):
                return FakeTable()

        monkeypatch.setattr(quac_worker, "_get_dynamodb", lambda: FakeResource())

        quac_worker._update_job_status("job-x", "completed", unless_cancelled=True)

        assert captured["ConditionExpression"] == "#status <> :cancelled"
        assert captured["ExpressionAttributeValues"][":cancelled"] == "cancelled"

    def test_unguarded_update_has_no_condition(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        class FakeTable:
            def update_item(self, **kwargs):
                captured.update(kwargs)
                return {}

        class FakeResource:
            def Table(self, name):
                return FakeTable()

        monkeypatch.setattr(quac_worker, "_get_dynamodb", lambda: FakeResource())

        quac_worker._update_job_status("job-y", "processing")

        assert "ConditionExpression" not in captured
