"""Deterministic backend selection — pure logic, no simulator imports.

The selector turns circuit structure + execution-environment facts into a
backend choice with human-readable reasons. It is shared verbatim by:

* the API's pre-flight analyzer (``report["recommendation"]``), and
* the worker's execution path (``worker.simulator.run_simulation``),

so the backend shown before submission is exactly the backend that executes.

Selection rules (evaluated in order, each with a recorded reason)
-----------------------------------------------------------------
1. **Clifford circuits** go to the stabilizer backend — exact simulation with
   polynomial cost — unless a noise model is present (Aer's stabilizer method
   does not support general noise models).
2. Circuits **wider than** :data:`~core.backends.base.STATEVECTOR_MAX_QUBITS`
   go to MPS: the dense statevector method would allocate more memory than
   its guard allows.
3. **Shallow** circuits (depth <= ``SHALLOW_DEPTH_THRESHOLD``) prefer MPS —
   tensor-network memory tracks entanglement, not width.
4. **Deep** circuits use the dense statevector method; when a GPU is actually
   detected the GPU variant is preferred (same exact method, faster).

A manual override is validated against the same capability facts — never
silently coerced — and rejected with a typed, actionable error when the
backend cannot run the circuit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from qiskit import QuantumCircuit

from core.backends.base import (
    CAPABILITIES,
    SHALLOW_DEPTH_THRESHOLD,
    STATEVECTOR_MAX_QUBITS,
    CapacitySignals,
    choose_simulation_method,
    clifford_refusal,
    detect_gpu,
    statevector_memory_bytes,
    worker_memory_budget_mb,
)

logger = logging.getLogger("quac.backends.selector")

#: Default transient-memory ceiling when the environment reports nothing —
#: matches the 2 GB worker Lambda the cost model already assumes.
DEFAULT_MEMORY_BUDGET_MB: int = 2048

#: The statevector method may claim at most this share of the worker memory
#: budget (transient transpiler/Aer workspace needs the rest).
STATEVECTOR_BUDGET_SHARE: float = 0.5

BACKEND_NAMES: tuple[str, ...] = ("stabilizer", "mps", "statevector", "gpu_statevector")


class BackendUnavailableError(RuntimeError):
    """Raised when the requested backend cannot run the circuit.

    The message names the backend and the exact blocking fact so callers can
    act on it (choose another backend, reduce width, remove the gate …).
    """

    def __init__(self, backend: str, reason: str) -> None:
        self.backend = backend
        self.reason = reason
        super().__init__(f"backend '{backend}' cannot run this circuit: {reason}")


@dataclass(frozen=True, slots=True)
class BackendSelection:
    """The outcome of :func:`select_backend`."""

    backend: str
    method: str
    device: str
    reasons: list[str]
    considered: list[str]
    caveats: list[str] = field(default_factory=list)


def _memory_budget_mb(capacity: CapacitySignals | None) -> int:
    if capacity is not None and capacity.memory_budget_mb is not None:
        return capacity.memory_budget_mb
    reported = worker_memory_budget_mb()
    return reported if reported is not None else DEFAULT_MEMORY_BUDGET_MB


def _validate_override(
    circuit: QuantumCircuit,
    requested: str,
    *,
    noise_model_present: bool,
    budget_mb: int,
    gpu_available: bool,
    gpu_reason: str,
) -> None:
    """Raise :class:`BackendUnavailableError` when an override cannot run."""
    if requested not in BACKEND_NAMES:
        raise BackendUnavailableError(
            requested, f"unknown backend; valid names are {list(BACKEND_NAMES)}"
        )

    capabilities = CAPABILITIES[requested]
    if capabilities.device == "gpu" and not gpu_available:
        raise BackendUnavailableError(requested, gpu_reason)

    if capabilities.requires_clifford:
        refusal = clifford_refusal(circuit)
        if refusal is not None:
            raise BackendUnavailableError(requested, refusal)
        if noise_model_present:
            raise BackendUnavailableError(
                requested,
                "the stabilizer method does not support general noise models",
            )

    if circuit.num_qubits > capabilities.max_qubits:
        raise BackendUnavailableError(
            requested,
            f"circuit width {circuit.num_qubits} exceeds the backend limit "
            f"of {capabilities.max_qubits} qubits",
        )

    if capabilities.aer_method == "statevector":
        required = statevector_memory_bytes(circuit.num_qubits)
        allowed = int(budget_mb * (1 << 20) * STATEVECTOR_BUDGET_SHARE)
        if required > allowed:
            raise BackendUnavailableError(
                requested,
                f"statevector array needs {required / (1 << 20):.0f} MiB but the "
                f"worker memory budget ({budget_mb} MiB) only allows "
                f"{allowed / (1 << 20):.0f} MiB for the vector",
            )


def select_backend(
    circuit: QuantumCircuit,
    *,
    shots: int = 1024,
    noise_model_present: bool = False,
    capacity: CapacitySignals | None = None,
    requested_backend: str | None = None,
) -> BackendSelection:
    """Choose the backend that will execute ``circuit``.

    Args:
            circuit: the circuit that would be executed (same object the
                    worker receives — selection and execution cannot drift).
            shots: requested sample count. Recorded for contract symmetry;
                    Aer draws samples after a single state evolution, so
                    shot count does not change the method choice.
            noise_model_present: excludes the stabilizer backend (P5 wiring).
            capacity: environment capacity signals; defaults to values read
                    from the current environment.
            requested_backend: manual override, validated (never coerced).

    Raises:
            BackendUnavailableError: when an override cannot run the circuit.
    """
    num_qubits = circuit.num_qubits
    depth = circuit.depth()
    budget_mb = _memory_budget_mb(capacity)
    refusal = clifford_refusal(circuit)
    considered: list[str] = []
    caveats: list[str] = []

    # ---- manual override: validate against the same facts, never coerce ----
    if requested_backend is not None:
        gpu_available, gpu_reason = detect_gpu()
        _validate_override(
            circuit,
            requested_backend,
            noise_model_present=noise_model_present,
            budget_mb=budget_mb,
            gpu_available=gpu_available,
            gpu_reason=gpu_reason,
        )
        capabilities = CAPABILITIES[requested_backend]
        logger.info(
            "manual backend override accepted backend=%s qubits=%d depth=%d",
            requested_backend,
            num_qubits,
            depth,
        )
        return BackendSelection(
            backend=requested_backend,
            method=capabilities.aer_method,
            device=capabilities.device,
            reasons=[
                f"manual override requested and validated: '{requested_backend}' "
                f"accepts this {num_qubits}-qubit circuit"
                + (" (Clifford)" if capabilities.requires_clifford else "")
            ],
            considered=[requested_backend],
        )

    # ---- 1. Clifford circuits: exact polynomial-cost stabilizer ----
    considered.append("stabilizer")
    if refusal is None and not noise_model_present:
        reasons = [
            f"all {len(circuit.data)} operations are Clifford "
            f"(depth {depth}, {num_qubits} qubits) -> stabilizer backend, "
            "exact simulation with cost polynomial in circuit size"
        ]
        if depth > SHALLOW_DEPTH_THRESHOLD:
            reasons.append(
                f"circuit is deep (depth {depth} > {SHALLOW_DEPTH_THRESHOLD}) but "
                "stability is exact for Clifford circuits — no dense statevector needed"
            )
        return BackendSelection(
            backend="stabilizer",
            method=CAPABILITIES["stabilizer"].aer_method,
            device="cpu",
            reasons=reasons,
            considered=considered,
            caveats=caveats,
        )
    if refusal is None and noise_model_present:
        caveats.append(
            "noise model present -> stabilizer backend excluded "
            "(Aer's stabilizer method does not support general noise models)"
        )

    # ---- 2. Width guard: only MPS survives beyond the statevector ceiling ----
    if num_qubits > STATEVECTOR_MAX_QUBITS:
        considered.append("statevector")
        considered.append("mps")
        reasons = [
            f"circuit width {num_qubits} exceeds the statevector backend limit of "
            f"{STATEVECTOR_MAX_QUBITS} qubits (16 B x 2^n memory guard) -> MPS"
        ]
        if refusal is not None:
            reasons.append(f"stabilizer unavailable: {refusal}")
        if depth > SHALLOW_DEPTH_THRESHOLD:
            caveats.append(
                f"depth {depth} exceeds the MPS-friendly threshold; large bond "
                "dimensions may slow the tensor-network method"
            )
        return BackendSelection(
            backend="mps",
            method=CAPABILITIES["mps"].aer_method,
            device="cpu",
            reasons=reasons,
            considered=considered,
            caveats=caveats,
        )

    # ---- 3. Shallow non-Clifford circuits: MPS exploits low entanglement ----
    if choose_simulation_method(circuit) == "matrix_product_state":
        considered.append("statevector")
        considered.append("mps")
        reasons = [
            f"depth {depth} <= {SHALLOW_DEPTH_THRESHOLD} -> matrix_product_state "
            "(tensor-network memory grows with entanglement, not width)"
        ]
        return BackendSelection(
            backend="mps",
            method=CAPABILITIES["mps"].aer_method,
            device="cpu",
            reasons=reasons,
            considered=considered,
            caveats=caveats,
        )

    # ---- 4. Deep non-Clifford circuits: dense statevector (GPU when real) ----
    considered.append("statevector")
    considered.append("mps")
    reasons = [
        f"depth {depth} > {SHALLOW_DEPTH_THRESHOLD} and width {num_qubits} <= "
        f"{STATEVECTOR_MAX_QUBITS} -> dense statevector "
        "(MPS bond dimensions would explode on deep circuits)"
    ]
    required_bytes = statevector_memory_bytes(num_qubits)
    allowed_bytes = int(budget_mb * (1 << 20) * STATEVECTOR_BUDGET_SHARE)
    if required_bytes > allowed_bytes:
        considered.append("gpu_statevector")
        reasons.append(
            f"worker memory budget ({budget_mb} MiB) caps the statevector vector at "
            f"{allowed_bytes / (1 << 20):.0f} MiB; {required_bytes / (1 << 20):.0f} MiB "
            "exceeds it -> MPS fallback"
        )
        return BackendSelection(
            backend="mps",
            method=CAPABILITIES["mps"].aer_method,
            device="cpu",
            reasons=reasons,
            considered=considered,
            caveats=caveats,
        )

    gpu_available, gpu_reason = detect_gpu()
    if gpu_available:
        considered.append("gpu_statevector")
        reasons.append(f"{gpu_reason} -> GPU statevector variant")
        return BackendSelection(
            backend="gpu_statevector",
            method=CAPABILITIES["gpu_statevector"].aer_method,
            device="gpu",
            reasons=reasons,
            considered=considered,
            caveats=caveats,
        )
    return BackendSelection(
        backend="statevector",
        method=CAPABILITIES["statevector"].aer_method,
        device="cpu",
        reasons=reasons,
        considered=considered,
        caveats=caveats,
    )
