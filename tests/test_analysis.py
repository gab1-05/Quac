"""Pre-flight analyzer tests (P2): exact metrics, basis labels, warnings,
cross-format consistency, worker parity and the analyze endpoint contract."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

pytest.importorskip("qiskit", reason="qiskit not installed")

from qiskit import QuantumCircuit
from qiskit.circuit import Parameter

from core import analysis as core_analysis
from core.analysis import (
    SHALLOW_DEPTH_THRESHOLD,
    analyze,
    analyze_quantum_circuit,
    choose_simulation_method,
)
from core.circuit import CircuitBuildError, build_circuit

GHZ3_QASM3 = (
    "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[3] q;\nbit[3] c;\n"
    "h q[0];\ncx q[0], q[1];\ncx q[1], q[2];\nc = measure q;\n"
)
CANCEL_PAIRS_QASM3 = (
    "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[2] q;\n"
    "h q[0];\nh q[0];\ncx q[0], q[1];\ncx q[0], q[1];\n"
)
MEASURE_BREAKS_CHAIN_QASM3 = (
    "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[1] q;\nbit[1] c;\n"
    "h q[0];\nc = measure q;\nh q[0];\n"
)
S_CHAIN_QASM3 = (
    "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[1] q;\ns q[0];\ns q[0];\n"
)
NO_MEASURE_QASM3 = (
    "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[4] q;\n"
    "h q[0];\ncx q[0], q[1];\ncz q[2], q[3];\n"
)
WIDE_QASM3 = "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[27] q;\nh q[0];\n"


class TestBellExactMetrics:
    """The Bell circuit's numbers are asserted absolutely (no tolerance)."""

    def test_bell_absolute_metrics(self, bell_qasm3: str) -> None:
        report = analyze(bell_qasm3, "qasm3", 1024)
        assert report["format"] == "qasm3"
        assert report["shots"] == 1024
        assert report["num_qubits"] == 2
        assert report["num_clbits"] == 2
        assert report["depth"] == 3
        assert report["critical_path"]["length"] == report["depth"] == 3
        assert report["arity"] == {"one_qubit": 1, "two_qubit": 1, "three_plus_qubit": 0}
        assert report["gate_histogram"] == {"h": 1, "cx": 1, "measure": 2}
        assert report["measurement_count"] == 2
        assert report["total_operations"] == 4
        assert report["two_qubit_density"] == 0.5
        assert report["executable"] is True
        assert report["parameter_count"] == 0

    def test_bell_interaction_proxies(self, bell_qasm3: str) -> None:
        interaction = analyze(bell_qasm3, "qasm3", 1024)["interaction"]
        assert interaction["edges"] == [[0, 1]]
        assert interaction["edge_operations"] == 1
        assert interaction["components"] == 1
        assert interaction["component_sizes"] == [2]
        assert interaction["max_component_qubits"] == 2
        assert interaction["bridge_edges"] == 1
        assert interaction["max_interaction_span"] == 1
        assert interaction["two_qubit_depth"] == 1
        assert "not entanglement" in interaction["note"]

    def test_bell_warnings_and_recommendation(self, bell_qasm3: str) -> None:
        report = analyze(bell_qasm3, "qasm3", 1024)
        assert report["warnings"] == []
        assert report["recommendation"]["method"] == "matrix_product_state"
        assert report["recommendation"]["reasons"]
        assert report["recommendation"]["caveats"] == []


class TestGhzAndGraphProxies:
    def test_ghz3_chain_proxies(self) -> None:
        report = analyze(GHZ3_QASM3, "qasm3", 1024)
        assert report["num_qubits"] == 3
        # h / cx / cx / measures — the last qubit can be measured in layer 3.
        assert report["depth"] == 4
        assert report["critical_path"]["length"] == 4
        interaction = report["interaction"]
        assert interaction["edges"] == [[0, 1], [1, 2]]
        assert interaction["components"] == 1
        assert interaction["bridge_edges"] == 2
        assert interaction["max_interaction_span"] == 1
        assert interaction["two_qubit_depth"] == 2

    def test_disconnected_components_counted(self) -> None:
        circuit = QuantumCircuit(6)
        circuit.cx(0, 1)
        circuit.cx(2, 3)
        interaction = analyze_quantum_circuit(circuit, 1024)["interaction"]
        assert interaction["components"] == 4  # two pairs + two isolated qubits
        assert interaction["component_sizes"] == [2, 2, 1, 1]


class TestEstimateLabels:
    def test_every_estimate_declares_its_basis(self, bell_qasm3: str) -> None:
        estimates = analyze(bell_qasm3, "qasm3", 1024)["estimates"]
        memory = estimates["statevector_memory_bytes"]
        assert memory["value"] == 16 * 2**2
        assert memory["basis"] == "exact"
        assert "16 bytes x 2^qubits" in memory["note"]

        runtime = estimates["runtime_seconds"]
        assert runtime["basis"] == "estimated"
        assert 0 < runtime["low"] <= runtime["high"]
        assert "heuristic" in runtime["note"]

        cost = estimates["compute_cost_usd"]
        assert cost["basis"] == "estimated"
        assert 0 < cost["low"] <= cost["high"]
        assert "verify current AWS pricing" in cost["note"]

        method = estimates["recommended_method"]
        assert method["basis"] == "rule"
        assert method["value"] == "matrix_product_state"

    def test_runtime_band_grows_with_width(self, bell_qasm3: str) -> None:
        small = analyze(bell_qasm3, "qasm3", 1024)["estimates"]["runtime_seconds"]
        wide = analyze(WIDE_QASM3, "qasm3", 1024)["estimates"]["runtime_seconds"]
        assert wide["low"] > small["high"]

    def test_deep_circuit_selects_statevector(self) -> None:
        deep = (
            "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[2] q;\n"
            + "h q[0];\n" * (SHALLOW_DEPTH_THRESHOLD + 1)
        )
        report = analyze(deep, "qasm3", 1024)
        assert report["depth"] == SHALLOW_DEPTH_THRESHOLD + 1
        assert report["estimates"]["recommended_method"]["value"] == "statevector"
        assert report["recommendation"]["method"] == "statevector"
        codes = {warning["code"] for warning in report["warnings"]}
        assert "DEEP_CIRCUIT_STATEVECTOR" in codes

    def test_near_cap_and_memory_pressure_warnings(self) -> None:
        report = analyze(WIDE_QASM3, "qasm3", 512)
        assert report["num_qubits"] == 27
        codes = {warning["code"] for warning in report["warnings"]}
        assert "NEAR_QUBIT_CAP" in codes
        assert "MEMORY_PRESSURE" in codes
        assert report["estimates"]["statevector_memory_bytes"]["value"] == 16 * 2**27


class TestWarningsAndOpportunities:
    def test_adjacent_self_inverse_pairs_collapse(self) -> None:
        report = analyze(CANCEL_PAIRS_QASM3, "qasm3", 1024)
        opportunities = [
            w for w in report["warnings"] if w["code"] == "REDUNDANT_ADJACENT_GATES"
        ]
        assert len(opportunities) == 1
        assert opportunities[0]["severity"] == "opportunity"
        assert "2 adjacent self-inverse gate pair(s)" in opportunities[0]["message"]

    def test_measurement_breaks_cancellation_adjacency(self) -> None:
        report = analyze(MEASURE_BREAKS_CHAIN_QASM3, "qasm3", 1024)
        assert not [w for w in report["warnings"] if w["code"] == "REDUNDANT_ADJACENT_GATES"]
        assert report["gate_histogram"] == {"h": 2, "measure": 1}

    def test_non_involution_gates_never_cancel(self) -> None:
        report = analyze(S_CHAIN_QASM3, "qasm3", 1024)
        assert not [w for w in report["warnings"] if w["code"] == "REDUNDANT_ADJACENT_GATES"]

    def test_unused_qubits_and_missing_measurements_reported(self) -> None:
        report = analyze(NO_MEASURE_QASM3, "qasm3", 1024)
        codes = {warning["code"] for warning in report["warnings"]}
        assert "NO_MEASUREMENTS" in codes
        no_measure = next(w for w in report["warnings"] if w["code"] == "NO_MEASUREMENTS")
        assert no_measure["severity"] == "info"

    def test_barrier_does_not_change_depth_or_layering(self) -> None:
        without = analyze(GHZ3_QASM3, "qasm3", 1024)
        with_barrier = analyze(
            GHZ3_QASM3.replace("cx q[0], q[1];", "barrier q;\ncx q[0], q[1];"),
            "qasm3",
            1024,
        )
        assert with_barrier["depth"] == without["depth"] == 4
        assert with_barrier["critical_path"]["length"] == without["critical_path"]["length"]


class TestParameterizedCircuits:
    def test_unbound_parameters_flagged(self) -> None:
        circuit = QuantumCircuit(2)
        theta = Parameter("theta")
        circuit.ry(theta, 0)
        circuit.cx(0, 1)
        circuit.measure_all()
        report = analyze_quantum_circuit(circuit, 1024)
        assert report["parameter_count"] == 1
        assert report["unbound_parameter_names"] == ["theta"]
        assert report["executable"] is False
        assert "UNBOUND_PARAMETERS" in {w["code"] for w in report["warnings"]}


class TestCrossFormatConsistency:
    def test_qiskit_json_matches_qasm3_byte_for_byte(
        self, bell_qasm3: str, generic_qiskit_json: dict
    ) -> None:
        from_qasm = analyze(bell_qasm3, "qasm3", 1024)
        from_json = analyze(generic_qiskit_json, "qiskit_json", 1024)
        from_qasm.pop("format")
        from_json.pop("format")
        assert from_json == from_qasm

    def test_method_selection_rule_is_shared_with_the_worker(self) -> None:
        from worker import simulator as worker_simulator

        assert worker_simulator.choose_simulation_method is choose_simulation_method
        assert worker_simulator.choose_simulation_method is core_analysis.choose_simulation_method
        assert worker_simulator.SHALLOW_DEPTH_THRESHOLD == SHALLOW_DEPTH_THRESHOLD

    def test_worker_execution_sees_the_same_circuit(self, bell_qasm3: str) -> None:
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        from worker.simulator import run_simulation

        result = run_simulation(bell_qasm3, "qasm3", 128)
        report = analyze(bell_qasm3, "qasm3", 1024)
        assert result.depth == report["depth"]
        assert result.num_qubits == report["num_qubits"]
        assert result.most_frequent in {"00", "11"}

    def test_cirq_json_matches_qasm3_byte_for_byte(self, bell_qasm3: str) -> None:
        cirq = pytest.importorskip("cirq", reason="cirq-core not installed")
        q0, q1 = cirq.LineQubit.range(2)
        document = json.loads(
            cirq.to_json(cirq.Circuit(cirq.H(q0), cirq.CNOT(q0, q1), cirq.measure(q0, q1)))
        )
        from_cirq = analyze(document, "cirq_json", 1024)
        from_qasm = analyze(bell_qasm3, "qasm3", 1024)
        from_cirq.pop("format")
        from_qasm.pop("format")
        assert from_cirq == from_qasm


def _require_cirq() -> Any:
    """Import cirq or skip the test when cirq-core is not installed."""
    return pytest.importorskip("cirq", reason="cirq-core not installed")


def _cirq_document(circuit: Any) -> dict[str, Any]:
    """Serialise a real ``cirq.Circuit`` into the cirq_json payload shape."""
    cirq = _require_cirq()
    return json.loads(cirq.to_json(circuit))


class TestCirqTranslatorCorrection:
    """cirq_json happy path against real cirq 1.x class names (P2).

    The legacy translator keyed lower-cased short names (``"h"``, ``"cx"`` …)
    that no cirq class ever has — every non-measurement gate failed. It now
    maps the actual classes (``HPowGate``, ``_PauliX``, ``ZPowGate`` …) and
    rejects powers without an exact Qiskit equivalent instead of silently
    mistranslating them.
    """

    def test_bell_translates_with_no_angle_on_fixed_gates(self) -> None:
        cirq = _require_cirq()
        q0, q1 = cirq.LineQubit.range(2)
        document = _cirq_document(cirq.Circuit(cirq.H(q0), cirq.CNOT(q0, q1)))
        circuit = build_circuit(document, "cirq_json")
        names = [instruction.operation.name for instruction in circuit.data]
        assert names == ["h", "cx"]
        # Fixed gates must not be applied with the power as an angle.
        assert all(not instruction.operation.params for instruction in circuit.data)
        assert circuit.num_qubits == 2

    def test_phase_gates_translate_to_rz(self) -> None:
        cirq = _require_cirq()
        q0 = cirq.LineQubit(0)
        for source, expected in ((cirq.S, math.pi / 2), (cirq.T, math.pi / 4)):
            circuit = build_circuit(_cirq_document(cirq.Circuit(source(q0))), "cirq_json")
            operation = circuit.data[0].operation
            assert operation.name == "rz"
            assert operation.params[0] == pytest.approx(expected)

    def test_rotation_gate_keeps_radians(self) -> None:
        cirq = _require_cirq()
        q0 = cirq.LineQubit(0)
        circuit = build_circuit(_cirq_document(cirq.Circuit(cirq.rx(0.5)(q0))), "cirq_json")
        operation = circuit.data[0].operation
        assert operation.name == "rx"
        assert operation.params[0] == pytest.approx(0.5)

    def test_non_exact_powers_are_rejected_typed(self) -> None:
        cirq = _require_cirq()
        q0, q1 = cirq.LineQubit.range(2)
        for case in (
            cirq.Circuit(cirq.H(q0) ** 0.5),
            cirq.Circuit(cirq.CNOT(q0, q1) ** 0.5),
            cirq.Circuit(cirq.ISWAP(q0, q1) ** -1),
        ):
            with pytest.raises(CircuitBuildError, match="exponent"):
                build_circuit(_cirq_document(case), "cirq_json")

    def test_unknown_cirq_gate_is_still_rejected(self) -> None:
        cirq = _require_cirq()
        q0, q1 = cirq.LineQubit.range(2)
        document = _cirq_document(cirq.Circuit(cirq.FSimGate(0.5, 0.25)(q0, q1)))
        with pytest.raises(CircuitBuildError, match="unsupported cirq gate"):
            build_circuit(document, "cirq_json")

    def test_worker_executes_translated_cirq_bell(self) -> None:
        """The fixed translator feeds the real execution path end to end."""
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        cirq = _require_cirq()
        from worker.simulator import run_simulation

        q0, q1 = cirq.LineQubit.range(2)
        circuit = cirq.Circuit(cirq.H(q0), cirq.CNOT(q0, q1), cirq.measure(q0, q1))
        result = run_simulation(_cirq_document(circuit), "cirq_json", 256)
        assert result.num_qubits == 2
        assert sum(result.counts.values()) == 256
        assert set(result.counts) == {"00", "11"}


class TestAnalyzeEndpoint:
    def test_analyze_returns_report_with_zero_side_effects(
        self, client, stubbed_backend: dict, bell_qasm3: str
    ) -> None:
        response = client.post(
            "/v1/quantum/analyze",
            json={"circuit_data": bell_qasm3, "format": "qasm3", "shots": 1024},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["num_qubits"] == 2
        assert body["depth"] == 3
        assert body["estimates"]["statevector_memory_bytes"]["basis"] == "exact"
        # Zero side effects: nothing persisted, nothing enqueued.
        assert stubbed_backend["store"] == {}
        assert stubbed_backend["calls"]["create"] == []
        assert stubbed_backend["calls"]["enqueue"] == []

    def test_firewall_runs_before_analysis(self, client, stubbed_backend: dict) -> None:
        response = client.post(
            "/v1/quantum/analyze",
            json={
                "circuit_data": "OPENQASM 3.0;\ninclude <stdgates.inc>;\nqubit[1] q;\n",
                "format": "qasm3",
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "FILE_INCLUDE_BANNED"
        assert stubbed_backend["calls"] == {"create": [], "update": [], "enqueue": [], "cancel": []}

    def test_qubit_cap_enforced(self, client) -> None:
        response = client.post(
            "/v1/quantum/analyze",
            json={
                "circuit_data": "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[29] q;\n",
                "format": "qasm3",
            },
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["error"] == "QUBIT_LIMIT_EXCEEDED"
        assert detail["observed_qubits"] == 29
        assert detail["max_qubits"] == 28

    def test_firewall_clean_but_unparseable_circuit_maps_to_422(self, client) -> None:
        # The JSON firewall checks structure only; the shared parser still has
        # to refuse an unknown gate — surfaced as a typed 422, not a 500.
        response = client.post(
            "/v1/quantum/analyze",
            json={
                "circuit_data": {
                    "num_qubits": 1,
                    "instructions": [{"name": "warp_drive", "qubits": [0]}],
                },
                "format": "qiskit_json",
            },
        )
        assert response.status_code == 422
        assert response.json()["detail"]["error"] == "CIRCUIT_ANALYSIS_FAILED"
        assert "warp_drive" in response.json()["detail"]["message"]

    def test_analyze_accepts_bare_cirq_document(
        self, client, stubbed_backend: dict, bell_qasm3: str
    ) -> None:
        # The natural cirq.to_json() output must work at the endpoint with no
        # envelope, and agree byte-for-byte with the qasm3 report.
        cirq = _require_cirq()
        q0, q1 = cirq.LineQubit.range(2)
        circuit = cirq.Circuit(cirq.H(q0), cirq.CNOT(q0, q1), cirq.measure(q0, q1))
        document = _cirq_document(circuit)
        assert "moments" in document and "num_qubits" not in document

        response = client.post(
            "/v1/quantum/analyze",
            json={"circuit_data": document, "format": "cirq_json", "shots": 1024},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["num_qubits"] == 2
        assert body["depth"] == 3
        assert body["gate_histogram"] == {"h": 1, "cx": 1, "measure": 2}
        # Zero side effects: nothing persisted, nothing enqueued.
        assert stubbed_backend["store"] == {}
        assert stubbed_backend["calls"]["enqueue"] == []

        from_qasm = client.post(
            "/v1/quantum/analyze",
            json={"circuit_data": bell_qasm3, "format": "qasm3", "shots": 1024},
        ).json()
        body.pop("format")
        from_qasm.pop("format")
        assert body == from_qasm

    def test_unknown_request_fields_rejected(self, client) -> None:
        response = client.post(
            "/v1/quantum/analyze",
            json={
                "circuit_data": "OPENQASM 3.0;\nqubit[1] q;\n",
                "format": "qasm3",
                "shots": 1024,
                "surprise": True,
            },
        )
        assert response.status_code == 422
