"""Circuit validation firewall tests (Challenge #2)."""

from __future__ import annotations

import pytest
from conftest import (
    BELL_QASM3,
    COMMENT_SMUGGLE,
    MALICIOUS_DEF,
    MALICIOUS_INCLUDE,
    MALICIOUS_LOOP,
    QASM3_29_QUBITS,
)
from pydantic import ValidationError

from api.schemas import (
    MAX_CIRCUIT_BYTES,
    MAX_QUBITS,
    CircuitFormat,
    CircuitValidationError,
    QubitLimitExceededError,
    SimulationRequest,
    validate_circuit_payload,
)

# --------------------------------------------------------------------------------------
# Accept path
# --------------------------------------------------------------------------------------


class TestFirewallAccepts:
    def test_bell_state_accepted(self, bell_qasm3: str) -> None:
        assert validate_circuit_payload(bell_qasm3, CircuitFormat.QASM3) == 2

    def test_ghz_accepted(self, ghz5_qasm3: str) -> None:
        assert validate_circuit_payload(ghz5_qasm3, CircuitFormat.QASM3) == 5

    def test_comment_smuggle_is_inert_and_accepted(self) -> None:
        # Commented-out malicious statements are stripped before analysis.
        assert validate_circuit_payload(COMMENT_SMUGGLE, CircuitFormat.QASM3) == 1

    def test_qreg_qasm2_declaration_counted(self) -> None:
        source = "OPENQASM 2.0;\nqreg q[3];\ncreg c[3];\nh q[0];\n"
        assert validate_circuit_payload(source, CircuitFormat.QASM3) == 3

    def test_single_implicit_qubit_declaration(self) -> None:
        source = "OPENQASM 3.0;\nqubit q;\nh q[0];\n"
        assert validate_circuit_payload(source, CircuitFormat.QASM3) == 1

    def test_qiskit_json_accepted(self, generic_qiskit_json: dict) -> None:
        assert (
            validate_circuit_payload(generic_qiskit_json, CircuitFormat.QISKIT_JSON) == 2
        )

    def test_cirq_json_qubit_count(self) -> None:
        payload = {"num_qubits": 4, "circuit": {"some": "cirq document"}}
        assert validate_circuit_payload(payload, CircuitFormat.CIRQ_JSON) == 4


# --------------------------------------------------------------------------------------
# Qubit cost firewall (28-qubit ceiling)
# --------------------------------------------------------------------------------------


class TestQubitCostFirewall:
    def test_boundary_28_qubits_accepted(self) -> None:
        source = "OPENQASM 3.0;\nqubit[28] q;\nh q[0];\n"
        assert validate_circuit_payload(source, CircuitFormat.QASM3) == 28

    def test_29_qubits_rejected_with_code(self) -> None:
        with pytest.raises(QubitLimitExceededError) as excinfo:
            validate_circuit_payload(QASM3_29_QUBITS, CircuitFormat.QASM3)
        assert excinfo.value.code == "QUBIT_LIMIT_EXCEEDED"
        assert excinfo.value.observed == 29
        assert excinfo.value.limit == MAX_QUBITS

    def test_json_payload_qubit_limit(self) -> None:
        payload = {"num_qubits": 40, "instructions": [{"name": "h", "qubits": [0]}]}
        with pytest.raises(QubitLimitExceededError):
            validate_circuit_payload(payload, CircuitFormat.QISKIT_JSON)


# --------------------------------------------------------------------------------------
# Denylist: injection and DoS vectors
# --------------------------------------------------------------------------------------


class TestFirewallRejects:
    @pytest.mark.parametrize(
        ("payload", "expected_code"),
        [
            (MALICIOUS_INCLUDE, "FILE_INCLUDE_BANNED"),
            (MALICIOUS_LOOP, "UNBOUNDED_LOOP_BANNED"),
            (MALICIOUS_DEF, "CUSTOM_DEFINITION_BANNED"),
        ],
    )
    def test_banned_constructs_rejected(self, payload: str, expected_code: str) -> None:
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload(payload, CircuitFormat.QASM3)
        assert excinfo.value.code == expected_code

    def test_rejection_reports_line_number(self) -> None:
        source = "OPENQASM 3.0;\nqubit[1] q;\nfor (int i = 0; i < 999; i++) { h q[0]; }\n"
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload(source, CircuitFormat.QASM3)
        assert excinfo.value.line == 3

    def test_pragma_rejected(self) -> None:
        source = 'OPENQASM 3.0;\npragma qasm3_import "os";\nqubit[1] q;\n'
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload(source, CircuitFormat.QASM3)
        assert excinfo.value.code == "PRAGMA_BANNED"

    def test_unknown_statement_rejected(self) -> None:
        source = "OPENQASM 3.0;\nqubit[1] q;\nlaunch_missiles q[0];\n"
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload(source, CircuitFormat.QASM3)
        assert excinfo.value.code == "STATEMENT_NOT_ALLOWED"

    def test_oversized_payload_rejected(self) -> None:
        filler = "OPENQASM 3.0;\n" + ("// pad\n" * 4) + "qubit[1] q;\nh q[0];\n"
        oversized = filler + ("// " + "x" * 1024 + "\n") * 100
        assert len(oversized.encode("utf-8")) > MAX_CIRCUIT_BYTES
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload(oversized, CircuitFormat.QASM3)
        assert excinfo.value.code == "PAYLOAD_TOO_LARGE"

    def test_no_qubit_declaration_rejected(self) -> None:
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload("OPENQASM 3.0;\nh q[0];\n", CircuitFormat.QASM3)
        assert excinfo.value.code == "NO_QUBITS_DECLARED"

    def test_wrong_type_for_format_rejected(self) -> None:
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload({"num_qubits": 1}, CircuitFormat.QASM3)
        assert excinfo.value.code == "PAYLOAD_TYPE_MISMATCH"

    def test_qiskit_json_disallowed_key_rejected(self, generic_qiskit_json: dict) -> None:
        generic_qiskit_json["__proto__"] = "evil"
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload(generic_qiskit_json, CircuitFormat.QISKIT_JSON)
        assert excinfo.value.code == "JSON_KEY_NOT_ALLOWED"

    def test_deep_json_nesting_rejected(self) -> None:
        nested: dict = {"num_qubits": 1}
        cursor = nested
        for _ in range(20):
            cursor["child"] = {}
            cursor = cursor["child"]
        with pytest.raises(CircuitValidationError) as excinfo:
            validate_circuit_payload(nested, CircuitFormat.QISKIT_JSON)
        assert excinfo.value.code == "PAYLOAD_TOO_DEEP"


# --------------------------------------------------------------------------------------
# SimulationRequest contract validation
# --------------------------------------------------------------------------------------


class TestSimulationRequestContract:
    def _base(self, **overrides) -> dict:
        payload = {
            "circuit_data": BELL_QASM3,
            "format": "qasm3",
            "webhook_url": "https://consumer.example.com/hook",
        }
        payload.update(overrides)
        return payload

    def test_defaults(self) -> None:
        request = SimulationRequest.model_validate(self._base())
        assert request.shots == 1024
        assert request.idempotency_key is None

    @pytest.mark.parametrize("shots", [0, 8193, -5])
    def test_shots_bounds(self, shots: int) -> None:
        with pytest.raises(ValidationError):
            SimulationRequest.model_validate(self._base(shots=shots))

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://consumer.example.com/hook",
            "javascript:alert(1)",
            "https://user:pass@consumer.example.com/hook",
        ],
    )
    def test_webhook_url_strictness(self, url: str) -> None:
        with pytest.raises(ValidationError):
            SimulationRequest.model_validate(self._base(webhook_url=url))

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            SimulationRequest.model_validate(self._base(admin=True))

    def test_idempotency_key_length_cap(self) -> None:
        with pytest.raises(ValidationError):
            SimulationRequest.model_validate(self._base(idempotency_key="k" * 129))

    def test_invalid_format_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SimulationRequest.model_validate(self._base(format="qasm7"))
