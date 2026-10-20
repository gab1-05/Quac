"""P3 backend layer: Clifford classification, capability matrix, selector, execution.

The pure layers (classification, capabilities, selector) require only
``qiskit``; execution tests additionally require ``qiskit-aer`` and import
the worker inside the test, so environments without Aer still exercise the
selection logic.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

pytest.importorskip("qiskit", reason="qiskit not installed")

from qiskit import QuantumCircuit
from qiskit.circuit import Parameter

from core.backends import (
    BACKEND_NAMES,
    CAPABILITIES,
    STATEVECTOR_MAX_QUBITS,
    capabilities_matrix,
    clifford_refusal,
    detect_gpu,
    is_clifford,
    select_backend,
)
from core.backends.classical import (
    AerBackend,
    BackendExecutionError,
    get_backend,
)
from core.backends.selector import BackendUnavailableError

RY_QASM3 = (
    "OPENQASM 3.0;\n"
    'include "stdgates.inc";\n'
    "qubit[2] q;\n"
    "bit[2] c;\n"
    "ry(0.3) q[0];\n"
    "cx q[0], q[1];\n"
    "c = measure q;\n"
)

GHZ5_QASM3 = (
    "OPENQASM 3.0;\n"
    'include "stdgates.inc";\n'
    "qubit[5] q;\n"
    "bit[5] c;\n"
    "h q[0];\n"
    "cx q[0], q[1];\n"
    "cx q[1], q[2];\n"
    "cx q[2], q[3];\n"
    "cx q[3], q[4];\n"
    "c = measure q;\n"
)


def _clifford_circuit() -> QuantumCircuit:
    """A spread of exact Clifford operations (including Clifford rotations)."""
    circuit = QuantumCircuit(3)
    circuit.h(0)
    circuit.s(1)
    circuit.sdg(1)
    circuit.sx(0)
    circuit.sxdg(0)
    circuit.cz(0, 1)
    circuit.swap(1, 2)
    circuit.rz(math.pi / 2, 2)
    circuit.rx(-math.pi / 2, 0)
    circuit.measure_all()
    return circuit


def _deep_non_clifford(qubits: int, rounds: int) -> QuantumCircuit:
    circuit = QuantumCircuit(qubits)
    for _ in range(rounds):
        for target in range(qubits):
            circuit.ry(0.3, target)
        for target in range(qubits - 1):
            circuit.cx(target, target + 1)
    return circuit


class TestCliffordClassification:
    def test_clifford_circuits_are_accepted(self) -> None:
        assert clifford_refusal(_clifford_circuit()) is None
        assert is_clifford(_clifford_circuit()) is True

    @pytest.mark.parametrize(
        ("build", "gate"),
        [
            (lambda c: c.ry(0.1, 0), "ry"),
            (lambda c: c.t(0), "t"),
            (lambda c: c.rx(0.1, 0), "rx"),
            (lambda c: c.cp(0.3, 0, 1), "cp"),
            (lambda c: c.ccx(0, 1, 2), "ccx"),
            (lambda c: c.u(0.1, 0.2, 0.3, 0), "u"),
        ],
    )
    def test_non_clifford_gates_refused_with_gate_name(
        self, build: Any, gate: str
    ) -> None:
        circuit = QuantumCircuit(3)
        build(circuit)
        refusal = clifford_refusal(circuit)
        assert refusal is not None
        assert gate in refusal
        assert is_clifford(circuit) is False

    @pytest.mark.parametrize("angle", [0.0, math.pi / 2, math.pi, -math.pi / 2, 2 * math.pi])
    def test_clifford_rotation_angles_accepted(self, angle: float) -> None:
        circuit = QuantumCircuit(1)
        circuit.rz(angle, 0)
        assert clifford_refusal(circuit) is None

    def test_non_clifford_rotation_angle_refused(self) -> None:
        circuit = QuantumCircuit(1)
        circuit.rz(math.pi / 4, 0)  # T angle — not in the Clifford group
        refusal = clifford_refusal(circuit)
        assert refusal is not None
        assert "pi/2" in refusal

    def test_symbolic_angle_refused(self) -> None:
        circuit = QuantumCircuit(1)
        circuit.ry(Parameter("theta"), 0)
        refusal = clifford_refusal(circuit)
        assert refusal is not None
        assert "symbolic" in refusal

    def test_control_flow_operations_refused(self) -> None:
        circuit = QuantumCircuit(1, 1)
        circuit.measure(0, 0)
        with circuit.if_test((circuit.clbits[0], True)):
            circuit.x(0)
        refusal = clifford_refusal(circuit)
        assert refusal is not None
        assert "if_else" in refusal


class TestCapabilityMatrix:
    def test_matrix_covers_every_registered_backend(self) -> None:
        matrix = capabilities_matrix()
        assert set(matrix) == set(BACKEND_NAMES) == set(CAPABILITIES)
        for entry in matrix.values():
            assert entry["name"] in BACKEND_NAMES
            assert entry["aer_method"]
            assert entry["device"] in {"cpu", "gpu"}
            assert entry["max_qubits"] > 0
            assert entry["note"]
            assert isinstance(entry["available"], bool)

        assert matrix["statevector"]["max_qubits"] == STATEVECTOR_MAX_QUBITS == 16
        assert matrix["gpu_statevector"]["max_qubits"] == STATEVECTOR_MAX_QUBITS
        assert matrix["stabilizer"]["requires_clifford"] is True
        assert matrix["mps"]["requires_clifford"] is False
        assert matrix["stabilizer"]["aer_method"] == "stabilizer"
        assert matrix["mps"]["aer_method"] == "matrix_product_state"

    def test_gpu_undetected_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("QUAC_GPU_ENABLED", raising=False)
        available, reason = detect_gpu()
        assert available is False
        assert "QUAC_GPU_ENABLED" in reason
        entry = capabilities_matrix()["gpu_statevector"]
        assert entry["available"] is False
        assert entry["unavailable_reason"] is not None

    def test_gpu_opt_in_still_requires_nvidia_smi(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUAC_GPU_ENABLED", "1")
        monkeypatch.setattr("core.backends.base.shutil.which", lambda _name: None)
        available, reason = detect_gpu()
        assert available is False
        assert "nvidia-smi" in reason

    def test_gpu_detected_when_opted_in_and_probed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Probe:
            stdout = "GPU 0: NVIDIA GeForce RTX (UUID: GPU-0000)"

        monkeypatch.setenv("QUAC_GPU_ENABLED", "1")
        monkeypatch.setattr("core.backends.base.shutil.which", lambda _name: "/usr/bin/nvidia-smi")
        monkeypatch.setattr("core.backends.base.subprocess.run", lambda *a, **k: _Probe())
        available, reason = detect_gpu()
        assert available is True
        assert "1 device" in reason
        assert capabilities_matrix()["gpu_statevector"]["available"] is True

        # …and the deep non-Clifford path then routes to the GPU variant.
        selection = select_backend(_deep_non_clifford(3, 30))
        assert selection.backend == "gpu_statevector"
        assert selection.device == "gpu"
        assert any("GPU" in reason for reason in selection.reasons)


class TestSelectorReasoning:
    def test_shallow_non_clifford_prefers_mps(self) -> None:
        circuit = QuantumCircuit(2)
        circuit.ry(0.3, 0)
        circuit.cx(0, 1)
        circuit.measure_all()
        selection = select_backend(circuit)
        assert selection.backend == "mps"
        assert selection.method == "matrix_product_state"
        assert selection.device == "cpu"
        assert selection.considered == ["stabilizer", "statevector", "mps"]
        assert any("depth" in reason for reason in selection.reasons)
        assert selection.caveats == []

    def test_deep_non_clifford_selects_statevector(self) -> None:
        selection = select_backend(_deep_non_clifford(3, 30))
        assert selection.backend == "statevector"
        assert selection.method == "statevector"
        assert "stabilizer" in selection.considered
        assert any("depth" in reason and "statevector" in reason for reason in selection.reasons)

    def test_wide_circuit_is_forced_to_mps(self) -> None:
        circuit = _deep_non_clifford(18, 25)
        assert circuit.num_qubits > STATEVECTOR_MAX_QUBITS
        selection = select_backend(circuit)
        assert selection.backend == "mps"
        assert any("exceeds the statevector backend limit" in r for r in selection.reasons)
        # Deep + wide: the bond-dimension risk must surface as a caveat.
        assert selection.caveats
        assert any("bond" in caveat for caveat in selection.caveats)

    def test_wide_clifford_circuit_still_uses_stabilizer(self) -> None:
        circuit = QuantumCircuit(20)
        circuit.h(0)
        for index in range(19):
            circuit.cx(index, index + 1)
        circuit.measure_all()
        selection = select_backend(circuit)
        assert selection.backend == "stabilizer"
        assert any("Clifford" in reason for reason in selection.reasons)

    def test_memory_budget_forces_mps_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1 MiB budget allows only 0.5 MiB for the vector; a 16-qubit dense
        # statevector needs 1 MiB — the capacity signal must win.
        monkeypatch.setenv("QUAC_MEMORY_BUDGET_MB", "1")
        selection = select_backend(_deep_non_clifford(16, 21))
        assert selection.backend == "mps"
        assert any("worker memory budget" in reason for reason in selection.reasons)

    def test_noise_model_excludes_stabilizer(self) -> None:
        circuit = QuantumCircuit(2)
        circuit.h(0)
        circuit.cx(0, 1)
        circuit.measure_all()
        selection = select_backend(circuit, noise_model_present=True)
        assert selection.backend == "mps"
        assert any("stabilizer" in caveat and "noise" in caveat for caveat in selection.caveats)

    def test_shot_count_does_not_change_the_choice(self) -> None:
        circuit = _deep_non_clifford(2, 25)  # depth ~50 > 40 -> deep branch
        low = select_backend(circuit, shots=1)
        high = select_backend(circuit, shots=8192)
        assert low.backend == high.backend == "statevector"
        assert low.reasons == high.reasons


class TestManualOverride:
    def test_valid_override_is_accepted_with_override_reason(self) -> None:
        circuit = QuantumCircuit(2)
        circuit.h(0)
        circuit.cx(0, 1)
        circuit.measure_all()
        selection = select_backend(circuit, requested_backend="mps")
        assert selection.backend == "mps"
        assert selection.method == "matrix_product_state"
        assert selection.considered == ["mps"]
        assert "manual override" in selection.reasons[0]

    def test_stabilizer_override_on_non_clifford_circuit_refused(self) -> None:
        circuit = _deep_non_clifford(2, 2)
        with pytest.raises(BackendUnavailableError) as excinfo:
            select_backend(circuit, requested_backend="stabilizer")
        assert excinfo.value.backend == "stabilizer"
        assert "Clifford" in str(excinfo.value)

    def test_statevector_override_beyond_width_refused(self) -> None:
        circuit = QuantumCircuit(STATEVECTOR_MAX_QUBITS + 1)
        circuit.ry(0.3, 0)
        with pytest.raises(BackendUnavailableError, match="backend limit"):
            select_backend(circuit, requested_backend="statevector")

    def test_gpu_override_refused_without_detection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("QUAC_GPU_ENABLED", raising=False)
        circuit = QuantumCircuit(2)
        circuit.ry(0.3, 0)
        with pytest.raises(BackendUnavailableError, match="GPU disabled"):
            select_backend(circuit, requested_backend="gpu_statevector")

    def test_unknown_backend_name_refused(self) -> None:
        circuit = QuantumCircuit(1)
        circuit.h(0)
        with pytest.raises(BackendUnavailableError, match="unknown backend"):
            select_backend(circuit, requested_backend="qpu_simulator")

    def test_stabilizer_override_refused_when_noise_present(self) -> None:
        circuit = QuantumCircuit(1)
        circuit.h(0)
        with pytest.raises(BackendUnavailableError, match="noise"):
            select_backend(
                circuit, requested_backend="stabilizer", noise_model_present=True
            )


class TestBackendRegistry:
    def test_registry_exposes_every_backend_name(self) -> None:
        for name in BACKEND_NAMES:
            backend = get_backend(name)
            assert isinstance(backend, AerBackend)
            assert backend.name == name

    def test_unknown_backend_lookup_is_typed(self) -> None:
        with pytest.raises(BackendExecutionError, match="unknown backend"):
            get_backend("does_not_exist")

    def test_accepts_refusals_mirror_the_selector(self) -> None:
        assert get_backend("stabilizer").accepts(_clifford_circuit()) is None
        refusal = get_backend("stabilizer").accepts(_deep_non_clifford(2, 2))
        assert refusal is not None and "Clifford" in refusal
        wide = QuantumCircuit(STATEVECTOR_MAX_QUBITS + 1)
        wide.ry(0.3, 0)
        refusal = get_backend("statevector").accepts(wide)
        assert refusal is not None and "backend limit" in refusal

    def test_mps_env_options_are_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core.backends.classical import _mps_options

        monkeypatch.setenv("QUAC_MPS_MAX_BOND_DIM", "32")
        monkeypatch.setenv("QUAC_AER_MAX_EXECUTION_SECONDS", "60")
        assert _mps_options() == {
            "matrix_product_state_max_bond_dim": 32,
            "max_execution_time": 60,
        }

        monkeypatch.setenv("QUAC_MPS_MAX_BOND_DIM", "not-a-number")
        assert _mps_options() == {"max_execution_time": 60}

        options = AerBackend("mps", options={"matrix_product_state_max_bond_dim": 8})
        assert options.capabilities()["options"] == {"matrix_product_state_max_bond_dim": 8}


class TestWorkerExecution:
    """End-to-end execution through ``worker.simulator.run_simulation`` (aer)."""

    def test_bell_runs_on_the_stabilizer_backend(self, bell_qasm3: str) -> None:
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        from worker.simulator import run_simulation

        result = run_simulation(bell_qasm3, "qasm3", 512)
        assert result.backend == "stabilizer"
        assert result.method == "stabilizer"
        assert result.selection_reasons
        assert sum(result.counts.values()) == 512
        assert set(result.counts) == {"00", "11"}
        assert result.metadata["device"] == "cpu"
        assert result.metadata["considered_backends"] == ["stabilizer"]

    def test_manual_override_flows_into_the_result(self, bell_qasm3: str) -> None:
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        from worker.simulator import run_simulation

        result = run_simulation(bell_qasm3, "qasm3", 256, backend="mps")
        assert result.backend == "mps"
        assert result.method == "matrix_product_state"
        assert "manual override" in result.selection_reasons[0]
        assert sum(result.counts.values()) == 256

    def test_override_refusal_raises_typed_execution_error(self) -> None:
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        from worker.simulator import SimulationExecutionError, run_simulation

        with pytest.raises(SimulationExecutionError, match="Clifford"):
            run_simulation(RY_QASM3, "qasm3", 64, backend="stabilizer")

    def test_statevector_memory_guard_blocks_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        from worker.simulator import SimulationExecutionError, run_simulation

        monkeypatch.setenv("QUAC_MEMORY_BUDGET_MB", "1")
        circuit = "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[16] q;\nbit[16] c;\n"
        circuit += "ry(0.3) q[0];\n" + "cx q[0], q[1];\n" + "c = measure q;\n"
        with pytest.raises(SimulationExecutionError, match="memory budget"):
            run_simulation(circuit, "qasm3", 16, backend="statevector")

    def test_stabilizer_and_statevector_agree_on_ghz(self) -> None:
        """Both methods must agree on the GHZ support and near-50/50 split."""
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        from worker.simulator import run_simulation

        stabilizer = run_simulation(GHZ5_QASM3, "qasm3", 1024)
        assert stabilizer.backend == "stabilizer"
        dense = run_simulation(GHZ5_QASM3, "qasm3", 1024, backend="statevector")
        assert dense.backend == "statevector"
        assert dense.method == "statevector"

        for result in (stabilizer, dense):
            assert set(result.counts) == {"00000", "11111"}
            assert sum(result.counts.values()) == 1024
            zero_fraction = result.counts["00000"] / 1024
            assert 0.4 < zero_fraction < 0.6  # ~0.5 +/- 6 sigma at 1024 shots

    def test_result_shape_is_backward_compatible(self) -> None:
        pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")
        from worker.simulator import SimulationResult

        legacy = SimulationResult(
            counts={"0": 1},
            shots=1,
            method="statevector",
            num_qubits=1,
            depth=1,
        )
        assert legacy.backend == ""
        assert legacy.selection_reasons == []
        assert legacy.most_frequent == "0"
