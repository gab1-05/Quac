"""SQS FIFO deduplication and queue-envelope tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from conftest import BELL_QASM3

from api.queue_service import (
    DEDUP_ID_MAX_LENGTH,
    build_message_body,
    canonical_payload_fingerprint,
    derive_deduplication_id,
)
from api.schemas import CircuitFormat, SimulationRequest


def _request(**overrides) -> SimulationRequest:
    payload = {
        "circuit_data": BELL_QASM3,
        "format": CircuitFormat.QASM3,
        "webhook_url": "https://consumer.example.com/hook",
    }
    payload.update(overrides)
    return SimulationRequest.model_validate(payload)


class TestDeduplicationId:
    def test_deterministic_for_identical_requests(self) -> None:
        first = derive_deduplication_id(_request())
        second = derive_deduplication_id(_request())
        assert first == second

    def test_always_within_sqs_length_ceiling(self) -> None:
        long_key = "x" * 128
        for request in (_request(), _request(idempotency_key=long_key)):
            dedup_id = derive_deduplication_id(request)
            assert len(dedup_id) <= DEDUP_ID_MAX_LENGTH
            assert len(dedup_id) == 64
            assert all(c in "0123456789abcdef" for c in dedup_id)

    def test_idempotency_key_takes_precedence(self) -> None:
        keyed = derive_deduplication_id(_request(idempotency_key="client-token"))
        unkeyed = derive_deduplication_id(_request())
        expected = hashlib.sha256(b"quac:idem:client-token").hexdigest()
        assert keyed == expected
        assert keyed != unkeyed

    def test_payload_fingerprint_branch(self) -> None:
        request = _request()
        expected = hashlib.sha256(
            f"quac:payload:{canonical_payload_fingerprint(request)}".encode()
        ).hexdigest()
        assert derive_deduplication_id(request) == expected

    @pytest.mark.parametrize(
        "overrides",
        [
            {"shots": 2048},
            {"circuit_data": BELL_QASM3 + "reset q[0];\n"},
            {"format": CircuitFormat.QISKIT_JSON, "circuit_data": {"num_qubits": 2}},
        ],
    )
    def test_different_content_different_dedup_id(self, overrides: dict) -> None:
        assert derive_deduplication_id(_request()) != derive_deduplication_id(
            _request(**overrides)
        )

    def test_fingerprint_ignores_key_ordering(self) -> None:
        # Semantic JSON ordering must not affect the digest.
        a = _request(circuit_data={"num_qubits": 2, "instructions": []})
        b = _request(circuit_data={"instructions": [], "num_qubits": 2})
        assert canonical_payload_fingerprint(a) == canonical_payload_fingerprint(b)


class TestMessageEnvelope:
    def test_envelope_round_trips(self) -> None:
        request = _request(idempotency_key="tok", shots=2048)
        body = build_message_body("job-123", request, 2)
        envelope = json.loads(body)

        assert envelope["job_id"] == "job-123"
        assert envelope["format"] == "qasm3"
        assert envelope["shots"] == 2048
        assert envelope["qubit_count"] == 2
        assert envelope["idempotency_key"] == "tok"
        assert envelope["webhook_url"] == "https://consumer.example.com/hook"
        assert envelope["schema_version"] == 1

    def test_envelope_contains_full_circuit(self) -> None:
        # SimulationRequest applies model-level whitespace stripping, so the
        # envelope carries the canonicalised (trimmed) circuit source.
        request = _request()
        envelope = json.loads(build_message_body("job-1", request, 2))
        assert envelope["circuit_data"] == BELL_QASM3.strip()
        assert envelope["circuit_data"] == request.circuit_data

    def test_envelope_is_single_line_json(self) -> None:
        # Compact separators are used (no pretty-printing); the circuit source
        # itself legitimately contains ", " inside gate arguments, so assert
        # on the encoding rather than substring absence.
        body = build_message_body("job-1", _request(), 2)
        assert "\n" not in body
        assert body == json.dumps(json.loads(body), separators=(",", ":"), ensure_ascii=True)
