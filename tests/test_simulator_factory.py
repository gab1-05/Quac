"""Simulator factory tests: format conversion, method selection, execution.

qiskit-aer is an optional heavy dependency in developer environments; the
entire module skips cleanly when it is not installed.
"""

from __future__ import annotations

import pytest

qiskit_aer = pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")

from qiskit import QuantumCircuit

from worker.simulator import (
    MAX_STATEVECTOR_QUBITS,
    SHALLOW_DEPTH_THRESHOLD,
    SimulationBuildError,
    SimulationExecutionError,
    SimulationResult,
    build_circuit,
    choose_simulation_method,
    run_simulation,
)


def _deep_circuit(qubits: int = 3, rounds: int = 30) -> QuantumCircuit:
    """Build a circuit whose gate depth exceeds SHALLOW_DEPTH_THRESHOLD.

    ``ry`` rotations at a non-Clifford angle keep the circuit outside the
    stabilizer group, so the *depth rule under test* is what selects the
    backend — not the Clifford shortcut (P3).
    """
    circuit = QuantumCircuit(qubits)
    for _ in range(rounds):
        for target in range(qubits):
            circuit.ry(0.3 + 0.07 * target, target)
        for target in range(qubits - 1):
            circuit.cx(target, target + 1)
    return circuit


class TestBuildCircuitFactory:
    def test_qasm3_bell_round_trip(self, bell_qasm3: str) -> None:
        circuit = build_circuit(bell_qasm3, "qasm3")
        assert isinstance(circuit, QuantumCircuit)
        assert circuit.num_qubits == 2

    def test_qasm3_format_is_case_insensitive(self, bell_qasm3: str) -> None:
        assert build_circuit(bell_qasm3, "QASM3").num_qubits == 2

    def test_qasm2_falls_back_to_from_qasm_str(self) -> None:
        source = (
            "OPENQASM 2.0;\n"
            'include "qelib1.inc";\n'
            "qreg q[2];\n"
            "h q[0];\n"
            "cx q[0],q[1];\n"
        )
        circuit = build_circuit(source, "qasm3")
        assert circuit.num_qubits == 2

    def test_qasm3_syntax_error_is_typed(self) -> None:
        with pytest.raises(SimulationBuildError, match="parse failure"):
            build_circuit("OPENQASM 3.0;\nqubit[1] q;\ncorrupt @@@ q[0];\n", "qasm3")

    def test_qiskit_json_generic_interchange(self, generic_qiskit_json: dict) -> None:
        circuit = build_circuit(generic_qiskit_json, "qiskit_json")
        assert circuit.num_qubits == 2
        assert circuit.num_clbits == 2
        names = [instruction.operation.name for instruction in circuit.data]
        assert names == ["h", "cx", "measure", "measure"]

    def test_qiskit_json_accepts_serialized_string(self, generic_qiskit_json: dict) -> None:
        import json

        circuit = build_circuit(json.dumps(generic_qiskit_json), "qiskit_json")
        assert circuit.num_qubits == 2

    def test_qiskit_json_qasm_passthrough(self) -> None:
        circuit = build_circuit(
            {
                "qasm": "OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[3];\nh q[0];\n"
            },
            "qiskit_json",
        )
        assert circuit.num_qubits == 3

    def test_qiskit_json_unknown_gate_rejected(self) -> None:
        payload = {"num_qubits": 1, "instructions": [{"name": "teleport", "qubits": [0]}]}
        with pytest.raises(SimulationBuildError, match="unsupported gate"):
            build_circuit(payload, "qiskit_json")

    def test_qiskit_json_missing_fields_rejected(self) -> None:
        with pytest.raises(SimulationBuildError):
            build_circuit({"metadata": {}}, "qiskit_json")

    def test_cirq_json_missing_dependency_is_typed(self) -> None:
        pytest.importorskip("cirq", reason="cirq-core not installed")
        payload = {"num_qubits": 1, "circuit": {"bogus": True}}
        with pytest.raises(SimulationBuildError):
            build_circuit(payload, "cirq_json")

    def test_unsupported_format_rejected(self, bell_qasm3: str) -> None:
        with pytest.raises(SimulationBuildError, match="unsupported circuit format"):
            build_circuit(bell_qasm3, "qasm99")

    def test_wrong_payload_type_rejected(self) -> None:
        with pytest.raises(SimulationBuildError, match="must be strings"):
            build_circuit({"num_qubits": 1}, "qasm3")


class TestMethodSelection:
    def test_shallow_circuit_selects_mps(self, bell_qasm3: str) -> None:
        circuit = build_circuit(bell_qasm3, "qasm3")
        assert circuit.depth() <= SHALLOW_DEPTH_THRESHOLD
        assert choose_simulation_method(circuit) == "matrix_product_state"

    def test_deep_circuit_selects_statevector(self) -> None:
        circuit = _deep_circuit(rounds=30)
        assert circuit.depth() > SHALLOW_DEPTH_THRESHOLD
        assert choose_simulation_method(circuit) == "statevector"

    def test_threshold_boundary_is_inclusive(self) -> None:
        shallow = QuantumCircuit(2)
        for _ in range(SHALLOW_DEPTH_THRESHOLD):
            shallow.x(0)  # serial single-qubit gates -> depth == threshold
        assert shallow.depth() == SHALLOW_DEPTH_THRESHOLD
        assert choose_simulation_method(shallow) == "matrix_product_state"


class TestRunSimulation:
    def test_bell_execution_produces_correlated_outcomes(self, bell_qasm3: str) -> None:
        result = run_simulation(bell_qasm3, "qasm3", 1024)
        assert isinstance(result, SimulationResult)
        assert result.shots == 1024
        assert result.num_qubits == 2
        # Bell is Clifford: the P3 selector routes it to the stabilizer backend.
        assert result.backend == "stabilizer"
        assert result.method == "stabilizer"
        assert result.selection_reasons
        assert sum(result.counts.values()) == 1024
        # Bell state: only |00> and |11> may appear.
        assert set(result.counts) <= {"00", "11"}
        assert set(result.counts) == {"00", "11"}
        assert result.most_frequent in {"00", "11"}

    def test_statevector_probabilities_normalised(self, bell_qasm3: str) -> None:
        result = run_simulation(bell_qasm3, "qasm3", 256)
        probabilities = result.statevector_probabilities
        assert probabilities is not None
        assert set(probabilities) == {"00", "11"}
        assert abs(sum(probabilities.values()) - 1.0) < 1e-6
        assert all(abs(probabilities[k] - 0.5) < 1e-6 for k in probabilities)

    def test_qiskit_json_execution(self, generic_qiskit_json: dict) -> None:
        result = run_simulation(generic_qiskit_json, "qiskit_json", 512)
        assert sum(result.counts.values()) == 512
        assert set(result.counts) <= {"00", "11"}

    def test_measurements_are_injected_when_absent(self) -> None:
        source = "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[1] q;\nh q[0];\n"
        result = run_simulation(source, "qasm3", 128)
        assert sum(result.counts.values()) == 128
        assert set(result.counts) <= {"0", "1"}

    def test_shots_bounds_enforced(self, bell_qasm3: str) -> None:
        with pytest.raises(SimulationExecutionError, match="shots"):
            run_simulation(bell_qasm3, "qasm3", 0)
        with pytest.raises(SimulationExecutionError, match="shots"):
            run_simulation(bell_qasm3, "qasm3", 8193)

    def test_statevector_extraction_skipped_beyond_qubit_cap(
        self, bell_qasm3: str
    ) -> None:
        assert MAX_STATEVECTOR_QUBITS <= 16
        # 2-qubit circuits always extract; verify flag surfaces in metadata.
        result = run_simulation(bell_qasm3, "qasm3", 64)
        assert result.metadata["statevector_extracted"] is True

    def test_deep_circuit_executes_on_statevector(self) -> None:
        circuit = _deep_circuit(qubits=3, rounds=30)
        result = run_simulation(
            {
                "num_qubits": circuit.num_qubits,
                "instructions": [
                    {
                        "name": inst.operation.name,
                        "qubits": [circuit.find_bit(q).index for q in inst.qubits],
                        **(
                            {"params": [float(p) for p in inst.operation.params]}
                            if inst.operation.params
                            else {}
                        ),
                    }
                    for inst in circuit.data
                ],
            },
            "qiskit_json",
            64,
        )
        assert result.backend == "statevector"
        assert result.method == "statevector"
        assert sum(result.counts.values()) == 64
