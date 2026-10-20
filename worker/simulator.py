"""Quac simulation factory — qiskit-aer execution engine.

Cold-start discipline (Challenge #1)
------------------------------------
Every heavyweight runtime package (``numpy``, ``qiskit``, ``qiskit_aer``,
``cirq``) is imported at **module scope**. In the AWS Lambda container
runtime the handler module is imported during the *initialization* phase,
i.e. outside the active handler loop, so the C++ extension linkage performed
by ``qiskit-aer`` is paid once per execution environment and never inside a
timed request. The Lambda handler itself performs zero heavy imports.

Responsibilities
----------------
* :func:`build_circuit` — format factory converting ``qasm3`` / ``qiskit_json``
  / ``cirq_json`` payloads into a ``QuantumCircuit``. The implementation lives
  in :mod:`core.circuit` (shared with the API's pre-flight analyzer so both
  planes parse a payload identically) and is re-exported here.
* :func:`choose_simulation_method` — depth-driven backend selection
  (``matrix_product_state`` for shallow circuits, ``statevector`` for dense
  entanglements). The rule lives in :mod:`core.analysis` and is re-exported.
* :func:`run_simulation` — transpile + execute + sample counts, with guarded
  statevector probability extraction for small registers.

Back-compat surface
-------------------
Historical import paths are preserved: ``worker.simulator.build_circuit``,
``worker.simulator.SimulationBuildError`` (alias of
``core.circuit.CircuitBuildError``) and
``worker.simulator.choose_simulation_method`` keep working for the worker's
callers and tests.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Final

# --------------------------------------------------------------------------------------
# Module-scope heavy imports — executed once during container initialization.
# --------------------------------------------------------------------------------------
import numpy as np
from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator

from core.analysis import SHALLOW_DEPTH_THRESHOLD, choose_simulation_method
from core.circuit import CircuitBuildError, build_circuit

logger = logging.getLogger("quac.worker.simulator")

# Historical alias: the format factory now raises core.circuit.CircuitBuildError.
SimulationBuildError = CircuitBuildError

# --------------------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------------------

MAX_STATEVECTOR_QUBITS: Final[int] = 16
"""Above this width probability extraction is skipped to bound memory growth."""

TRANSPILE_OPTIMIZATION_LEVEL: Final[int] = 2


class SimulationExecutionError(RuntimeError):
    """Raised when the Aer backend fails to execute a circuit."""


# --------------------------------------------------------------------------------------
# Result contract
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Immutable outcome of a single circuit execution."""

    counts: dict[str, int]
    shots: int
    method: str
    num_qubits: int
    depth: int
    statevector_probabilities: dict[str, float] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def most_frequent(self) -> str | None:
        """Return the outcome bitstring with the highest sample count."""
        if not self.counts:
            return None
        return max(self.counts.items(), key=lambda pair: pair[1])[0]


__all__ = [
    "MAX_STATEVECTOR_QUBITS",
    "SHALLOW_DEPTH_THRESHOLD",
    "TRANSPILE_OPTIMIZATION_LEVEL",
    "SimulationBuildError",
    "SimulationExecutionError",
    "SimulationResult",
    "build_circuit",
    "choose_simulation_method",
    "run_simulation",
]


def _ensure_measurements(circuit: QuantumCircuit) -> QuantumCircuit:
    """Guarantee the circuit measures its qubits into a classical register.

    * Circuits that already contain ``measure`` instructions pass through.
    * Circuits with no classical register get ``measure_all()`` applied.
    * Circuits that declare clbits but never measure map ``qubit[i] -> clbit[i]``
      for every overlapping pair.
    """
    has_measure = any(inst.operation.name == "measure" for inst in circuit.data)
    if has_measure:
        return circuit

    prepared = circuit.copy()
    if prepared.num_clbits == 0:
        prepared.measure_all()
        return prepared

    pair_count = min(prepared.num_qubits, prepared.num_clbits)
    if pair_count == 0:
        raise SimulationBuildError("circuit has neither qubits nor clbits to measure")
    for index in range(pair_count):
        prepared.measure(index, index)
    return prepared


def _extract_statevector_probabilities(circuit: QuantumCircuit) -> dict[str, float] | None:
    """Compute |amplitude|**2 per basis state for small registers only."""
    # remove_final_measurements(inplace=False) *returns* a stripped copy — the
    # return value must be assigned or save_statevector runs after collapse.
    probe = circuit.copy().remove_final_measurements(inplace=False)
    if probe.num_qubits > MAX_STATEVECTOR_QUBITS:
        logger.debug(
            "skipping statevector extraction qubits=%d max=%d",
            probe.num_qubits,
            MAX_STATEVECTOR_QUBITS,
        )
        return None

    try:
        probe.save_statevector()
        vector_sim = AerSimulator(method="statevector")
        compiled = transpile(probe, vector_sim, optimization_level=TRANSPILE_OPTIMIZATION_LEVEL)
        state = vector_sim.run(compiled, shots=1).result().get_statevector()
        amplitudes = np.asarray(state, dtype=np.complex128)
        probabilities = np.abs(amplitudes) ** 2
        total = float(probabilities.sum())
        if total <= 0 or not math.isfinite(total):
            return None
        normalised = probabilities / total
        return {
            format(index, f"0{probe.num_qubits}b"): round(float(value), 12)
            for index, value in enumerate(normalised)
            if value > 1e-12
        }
    except Exception as exc:  # noqa: BLE001 - diagnostics must not fail the job
        logger.warning("statevector extraction failed: %s", exc)
        return None


# --------------------------------------------------------------------------------------
# Execution entry point
# --------------------------------------------------------------------------------------


def run_simulation(
    circuit_data: str | dict[str, Any],
    circuit_format: str,
    shots: int,
) -> SimulationResult:
    """Build, transpile and execute a circuit on the qiskit-aer backend.

    Runs synchronously inside the worker's dedicated compute thread; all heavy
    package state was already initialised at module import time.

    Raises:
            SimulationBuildError: payload conversion failure.
            SimulationExecutionError: backend execution failure.
    """
    if shots < 1 or shots > 8192:
        raise SimulationExecutionError(f"shots must be within [1, 8192]; got {shots}")

    circuit = build_circuit(circuit_data, circuit_format)
    measured = _ensure_measurements(circuit)

    method = choose_simulation_method(circuit)
    # NOTE: optimization_level is a *transpile* parameter, not an Aer backend
    # option — passing it here raises AerError("Invalid option").
    simulator = AerSimulator(method=method)

    try:
        compiled = transpile(measured, simulator, optimization_level=TRANSPILE_OPTIMIZATION_LEVEL)
        job = simulator.run(compiled, shots=shots)
        aer_result = job.result()
        raw_counts: dict[str, int] = aer_result.get_counts()
    except Exception as exc:
        raise SimulationExecutionError(f"aer execution failure: {exc}") from exc

    counts = {
        str(bitstring).replace(" ", ""): int(count)
        for bitstring, count in raw_counts.items()
    }
    total = sum(counts.values())
    if total <= 0:
        raise SimulationExecutionError("backend returned an empty count distribution")

    probabilities = _extract_statevector_probabilities(circuit)
    num_qubits = circuit.num_qubits
    depth = circuit.depth()

    logger.info(
        "simulation finished qubits=%d depth=%d method=%s shots=%d outcomes=%d",
        num_qubits,
        depth,
        method,
        shots,
        len(counts),
    )

    return SimulationResult(
        counts=counts,
        shots=shots,
        method=method,
        num_qubits=num_qubits,
        depth=depth,
        statevector_probabilities=probabilities,
        metadata={
            "transpile_optimization_level": TRANSPILE_OPTIMIZATION_LEVEL,
            "shallow_depth_threshold": SHALLOW_DEPTH_THRESHOLD,
            "statevector_extracted": probabilities is not None,
        },
    )
