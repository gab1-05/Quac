"""Aer-backed simulator implementations — **worker-only** import.

This is the only module in :mod:`core` that imports ``qiskit_aer``; the API
plane must never import it (the API image deliberately does not ship Aer).
Selection logic lives in :mod:`core.backends.selector` and never constructs a
simulator, so the analyzer's recommendation stays pure metadata.

Every backend executes the *same measured circuit* the selector inspected:
transpilation happens here, once, against the concrete Aer simulator.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Final

from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator

from core.backends.base import (
    CAPABILITIES,
    clifford_refusal,
)

logger = logging.getLogger("quac.backends.classical")

TRANSPILE_OPTIMIZATION_LEVEL: Final[int] = 2
"""Qiskit transpile optimization level shared by every backend."""


class BackendExecutionError(RuntimeError):
    """Raised when an Aer backend fails to build, transpile or run a circuit."""


def _mps_options() -> dict[str, Any]:
    """Optional Aer MPS knobs, read once per simulator construction.

    Both are opt-in: unset means Aer defaults. ``QUAC_MPS_MAX_BOND_DIM``
    caps the tensor-network bond dimension (trades accuracy for memory);
    ``QUAC_AER_MAX_EXECUTION_SECONDS`` enables Aer's early-termination
    budget, which returns partial results when exceeded.
    """
    options: dict[str, Any] = {}
    bond = os.getenv("QUAC_MPS_MAX_BOND_DIM", "").strip()
    if bond.isdigit() and int(bond) > 0:
        options["matrix_product_state_max_bond_dim"] = int(bond)
    budget = os.getenv("QUAC_AER_MAX_EXECUTION_SECONDS", "").strip()
    if budget.isdigit() and int(budget) > 0:
        options["max_execution_time"] = int(budget)
    return options


class AerBackend:
    """One Aer simulation method behind the shared backend contract.

    The Aer simulator and its transpiler ``Target`` are built lazily once and
    reused: qiskit's ``transpile(circuit, backend)`` re-derives the backend
    Target on *every* access, which costs ~0.8 s per call on the pinned
    production stack (measured qiskit 1.4.2 / aer 0.15.1) versus ~0.1 s for a
    cached target — a fixed per-job overhead that dwarfs the simulation of
    small circuits. Sharing one simulator per backend name is safe because the
    worker executes jobs sequentially on its compute thread.
    """

    __slots__ = ("_device", "_method", "_name", "_options", "_simulator", "_target")

    def __init__(
        self,
        name: str,
        *,
        device: str = "cpu",
        options: dict[str, Any] | None = None,
    ) -> None:
        capabilities = CAPABILITIES[name]
        self._name = name
        self._method = capabilities.aer_method
        self._device = device
        self._options = dict(options or {})
        self._simulator: AerSimulator | None = None
        self._target: Any = None

    def _get_simulator(self) -> tuple[AerSimulator, Any]:
        """(simulator, transpiler target), built once and cached."""
        if self._simulator is None or self._target is None:
            # Aer accepts no ``device`` (CPU default) or ``device="GPU"`` —
            # the literal "cpu" is rejected as an invalid simulation device.
            simulator_kwargs: dict[str, Any] = dict(self._options)
            if self._device == "gpu":
                simulator_kwargs["device"] = "GPU"
            self._simulator = AerSimulator(method=self._method, **simulator_kwargs)
            self._target = self._simulator.target
        return self._simulator, self._target

    # ---- contract -----------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> dict[str, Any]:
        """Capability facts (static) plus the concrete options in effect."""
        from core.backends.base import capabilities_matrix  # local: keep import graph flat

        facts = dict(capabilities_matrix()[self._name])
        facts["options"] = dict(self._options)
        return facts

    def accepts(self, circuit: QuantumCircuit) -> str | None:
        """Refusal reason when this backend cannot run ``circuit``."""
        capabilities = CAPABILITIES[self._name]
        if circuit.num_qubits > capabilities.max_qubits:
            return (
                f"circuit width {circuit.num_qubits} exceeds the backend limit "
                f"of {capabilities.max_qubits} qubits"
            )
        if capabilities.requires_clifford:
            return clifford_refusal(circuit)
        return None

    def execute(self, circuit: QuantumCircuit, shots: int) -> dict[str, int]:
        """Transpile and run; returns the normalised counts histogram."""
        refusal = self.accepts(circuit)
        if refusal is not None:
            raise BackendExecutionError(f"backend '{self._name}' refuses circuit: {refusal}")
        try:
            simulator, target = self._get_simulator()
            # Transpile against the cached Target: passing the backend object
            # makes qiskit rebuild the Target on every access (measured ~0.84 s
            # fixed overhead per job on the pinned production stack).
            compiled = transpile(
                circuit, target=target, optimization_level=TRANSPILE_OPTIMIZATION_LEVEL
            )
            aer_result = simulator.run(compiled, shots=shots).result()
            raw_counts: dict[str, Any] = aer_result.get_counts()
        except Exception as exc:
            raise BackendExecutionError(f"backend '{self._name}' failed: {exc}") from exc

        counts = {
            str(bitstring).replace(" ", ""): int(count) for bitstring, count in raw_counts.items()
        }
        if sum(counts.values()) <= 0:
            raise BackendExecutionError(f"backend '{self._name}' returned empty counts")
        logger.info(
            "backend=%s device=%s executed qubits=%d shots=%d outcomes=%d",
            self._name,
            self._device,
            circuit.num_qubits,
            shots,
            len(counts),
        )
        return counts


def _build_registry() -> dict[str, AerBackend]:
    """Instantiate the backend registry (env-derived options applied here)."""
    return {
        "stabilizer": AerBackend("stabilizer"),
        "mps": AerBackend("mps", options=_mps_options()),
        "statevector": AerBackend("statevector"),
        "gpu_statevector": AerBackend("gpu_statevector", device="gpu"),
    }


BACKENDS: Final[dict[str, AerBackend]] = _build_registry()


def get_backend(name: str) -> AerBackend:
    """Look up a backend by selector name."""
    try:
        return BACKENDS[name]
    except KeyError as exc:
        raise BackendExecutionError(
            f"unknown backend '{name}'; valid names are {sorted(BACKENDS)}"
        ) from exc


__all__ = [
    "BACKENDS",
    "TRANSPILE_OPTIMIZATION_LEVEL",
    "AerBackend",
    "BackendExecutionError",
    "get_backend",
]
