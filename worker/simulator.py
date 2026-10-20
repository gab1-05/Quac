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
* :func:`select_backend` — the P3 backend selector (stabilizer / MPS /
  statevector / GPU statevector with human-readable reasons). The selector
  lives in :mod:`core.backends` and is shared verbatim with the analyzer's
  recommendation, so the backend shown up-front is the backend that runs.
* :func:`run_simulation` — select a backend, transpile + execute through the
  backend registry, sample counts, with guarded statevector probability
  extraction for small registers.

Back-compat surface
-------------------
Historical import paths are preserved: ``worker.simulator.build_circuit``,
``worker.simulator.SimulationBuildError`` (alias of
``core.circuit.CircuitBuildError``),
``worker.simulator.choose_simulation_method`` (the raw depth rule) and
``worker.simulator.SHALLOW_DEPTH_THRESHOLD`` keep working for the worker's
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

from core.backends import SHALLOW_DEPTH_THRESHOLD, choose_simulation_method, select_backend
from core.backends.base import STATEVECTOR_MAX_QUBITS
from core.backends.classical import (
    TRANSPILE_OPTIMIZATION_LEVEL,
    BackendExecutionError,
    get_backend,
)
from core.backends.selector import BackendUnavailableError
from core.circuit import CircuitBuildError, build_circuit

logger = logging.getLogger("quac.worker.simulator")

# Historical alias: the format factory now raises core.circuit.CircuitBuildError.
SimulationBuildError = CircuitBuildError

# --------------------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------------------

MAX_STATEVECTOR_QUBITS: Final[int] = STATEVECTOR_MAX_QUBITS
"""Above this width probability extraction is skipped to bound memory growth."""


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
    backend: str = ""
    """Backend name the selector chose (stabilizer | mps | statevector | gpu_statevector)."""
    selection_reasons: list[str] = field(default_factory=list)
    """Human-readable selection reasons (empty for documents predating P3)."""

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
    "select_backend",
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


_VECTOR_PROBE: tuple[AerSimulator, Any] | None = None
"""Cached (statevector simulator, transpiler Target) for probability extraction.

Same rationale as ``core.backends.classical.AerBackend._get_simulator``:
``transpile(circuit, backend)`` re-derives the backend Target on every access
(~0.8 s fixed overhead per job on the pinned production stack); the simulation
of small registers itself is milliseconds.
"""


def _get_vector_probe() -> tuple[AerSimulator, Any]:
    """Build the extraction simulator + Target once per worker process."""
    global _VECTOR_PROBE
    if _VECTOR_PROBE is None:
        simulator = AerSimulator(method="statevector")
        _VECTOR_PROBE = (simulator, simulator.target)
    return _VECTOR_PROBE


def _extract_statevector_probabilities(circuit: QuantumCircuit) -> dict[str, float] | None:
    """Compute |amplitude|**2 per basis state for small registers only."""
    # remove_final_measurements(inplace=False) *returns* a stripped copy — the
    # return value must be assigned or save_statevector runs after collapse.
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
        vector_sim, vector_target = _get_vector_probe()
        compiled = transpile(
            probe, target=vector_target, optimization_level=TRANSPILE_OPTIMIZATION_LEVEL
        )
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
    *,
    backend: str | None = None,
) -> SimulationResult:
    """Select a backend, then transpile and execute on the qiskit-aer engine.

    The backend selector runs first (the same one the analyzer recommends),
    unless ``backend`` names a manual override — overrides are validated
    against the capability facts and never silently coerced.

    Runs synchronously inside the worker's dedicated compute thread; all heavy
    package state was already initialised at module import time.

    Raises:
            SimulationBuildError: payload conversion failure.
            SimulationExecutionError: selection refusal or backend failure.
    """
    if shots < 1 or shots > 8192:
        raise SimulationExecutionError(f"shots must be within [1, 8192]; got {shots}")

    circuit = build_circuit(circuit_data, circuit_format)
    measured = _ensure_measurements(circuit)

    try:
        selection = select_backend(circuit, shots=shots, requested_backend=backend)
        counts = get_backend(selection.backend).execute(measured, shots)
    except (BackendUnavailableError, BackendExecutionError) as exc:
        raise SimulationExecutionError(str(exc)) from exc

    probabilities = _extract_statevector_probabilities(circuit)
    num_qubits = circuit.num_qubits
    depth = circuit.depth()

    logger.info(
        "simulation finished qubits=%d depth=%d backend=%s method=%s shots=%d outcomes=%d",
        num_qubits,
        depth,
        selection.backend,
        selection.method,
        shots,
        len(counts),
    )

    return SimulationResult(
        counts=counts,
        shots=shots,
        method=selection.method,
        num_qubits=num_qubits,
        depth=depth,
        statevector_probabilities=probabilities,
        metadata={
            "transpile_optimization_level": TRANSPILE_OPTIMIZATION_LEVEL,
            "shallow_depth_threshold": SHALLOW_DEPTH_THRESHOLD,
            "statevector_extracted": probabilities is not None,
            "device": selection.device,
            "considered_backends": selection.considered,
        },
        backend=selection.backend,
        selection_reasons=list(selection.reasons),
    )
