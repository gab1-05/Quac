"""Backend capability model — pure metadata and classification, no Aer imports.

Both planes import this module: the API's analyzer builds its recommendation
from the same capability facts the worker executes against, and neither the
API nor the analyzer may import ``qiskit_aer`` (it is a worker-only
dependency). Simulator construction lives in :mod:`core.backends.classical`,
which only the worker imports.

The constants that drive method selection historically lived in
:mod:`core.analysis`; they now live here so the selector and the analyzer can
share one definition without an import cycle. ``core.analysis`` re-exports
them, so every historical import path keeps working.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Final

from qiskit import QuantumCircuit

# --------------------------------------------------------------------------------------
# Selection constants (single source of truth; re-exported by core.analysis)
# --------------------------------------------------------------------------------------

SHALLOW_DEPTH_THRESHOLD: Final[int] = 40
"""Depth at or below which tensor-network (MPS) simulation is preferred."""

STATEVECTOR_MAX_QUBITS: Final[int] = 16
"""Statevector memory ceiling — 2^16 complex128 amplitudes = 1 MiB of vector."""

QUBIT_CAP: Final[int] = 28
"""Platform-wide qubit ceiling (matches the API's validation firewall)."""

#: Exact (angle-free) gates the stabilizer tableau can represent, plus the
#: classical instructions Aer's stabilizer method accepts.
CLIFFORD_GATES: Final[frozenset[str]] = frozenset(
    {
        "id",
        "x",
        "y",
        "z",
        "h",
        "s",
        "sdg",
        "sx",
        "sxdg",
        "cx",
        "cy",
        "cz",
        "swap",
        "iswap",
        "cswap",
        "measure",
        "barrier",
        "reset",
    }
)

#: Rotation gates that are Clifford *at specific angles* (multiples of pi/2,
#: e.g. ``rz(pi/2) == S``); any other angle is not in the Clifford group.
CLIFFORD_ROTATION_GATES: Final[frozenset[str]] = frozenset({"rx", "ry", "rz"})

CLIFFORD_ANGLE_TOLERANCE: Final[float] = 1e-9
"""Numerical tolerance for the ``k * pi/2`` Clifford-angle test."""


# --------------------------------------------------------------------------------------
# Capability facts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Immutable, JSON-ready description of one backend's contract."""

    name: str
    display_name: str
    aer_method: str
    device: str
    max_qubits: int
    requires_clifford: bool
    note: str


@dataclass(frozen=True, slots=True)
class CapacitySignals:
    """Execution-environment capacity inputs for the selector.

    ``memory_budget_mb`` bounds the transient memory the worker may claim;
    when unset the selector falls back to the platform default and records
    nothing about capacity in its reasons.
    """

    memory_budget_mb: int | None = None


def worker_memory_budget_mb() -> int | None:
    """Best-effort memory budget of the current execution environment.

    Reads ``AWS_LAMBDA_FUNCTION_MEMORY_SIZE`` (set automatically by Lambda)
    or ``QUAC_MEMORY_BUDGET_MB`` (explicit override for tests/other hosts).
    Returns ``None`` when neither is available — the selector then applies
    no capacity constraint rather than inventing one.
    """
    for variable in ("QUAC_MEMORY_BUDGET_MB", "AWS_LAMBDA_FUNCTION_MEMORY_SIZE"):
        raw = os.getenv(variable, "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    return None


def detect_gpu() -> tuple[bool, str]:
    """Report whether an Aer-compatible GPU can be used here.

    Two independent conditions must hold: the operator explicitly opted in
    with ``QUAC_GPU_ENABLED=1`` **and** ``nvidia-smi`` reports a device.
    Lambda and Fargate never satisfy this by default — the selector must
    never assume a GPU exists.
    """
    if os.getenv("QUAC_GPU_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return False, "GPU disabled (set QUAC_GPU_ENABLED=1 to opt in)"

    executable = shutil.which("nvidia-smi")
    if executable is None:
        return False, "GPU opted in but nvidia-smi was not found on PATH"

    try:
        probe = subprocess.run(
            [executable, "-L"], capture_output=True, text=True, timeout=5, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"nvidia-smi failed to report a device: {exc}"
    devices = [line for line in probe.stdout.splitlines() if line.strip()]
    if not devices:
        return False, "nvidia-smi reported no GPU devices"
    return True, f"GPU detected via nvidia-smi ({len(devices)} device(s))"


#: Backend registry — capability facts only; no simulator is constructed here.
CAPABILITIES: Final[dict[str, BackendCapabilities]] = {
    "stabilizer": BackendCapabilities(
        name="stabilizer",
        display_name="Stabilizer tableau simulator",
        aer_method="stabilizer",
        device="cpu",
        max_qubits=QUBIT_CAP,
        requires_clifford=True,
        note=(
            "Exact Clifford simulation with cost polynomial in circuit size; "
            "refuses any gate outside the Clifford group with a named reason."
        ),
    ),
    "mps": BackendCapabilities(
        name="mps",
        display_name="Matrix-product-state (tensor network) simulator",
        aer_method="matrix_product_state",
        device="cpu",
        max_qubits=QUBIT_CAP,
        requires_clifford=False,
        note=(
            "Memory grows with bond dimension (entanglement), not raw width; "
            "ideal for shallow circuits, can degrade on deep highly-entangled ones."
        ),
    ),
    "statevector": BackendCapabilities(
        name="statevector",
        display_name="Dense statevector simulator (CPU)",
        aer_method="statevector",
        device="cpu",
        max_qubits=STATEVECTOR_MAX_QUBITS,
        requires_clifford=False,
        note=(
            "Exact dense simulation; memory is 16 B x 2^qubits and is checked "
            "before allocation."
        ),
    ),
    "gpu_statevector": BackendCapabilities(
        name="gpu_statevector",
        display_name="Dense statevector simulator (GPU)",
        aer_method="statevector",
        device="gpu",
        max_qubits=STATEVECTOR_MAX_QUBITS,
        requires_clifford=False,
        note=(
            "Same exact statevector method on an accelerator; only offered when "
            "a GPU is actually detected (QUAC_GPU_ENABLED=1 + nvidia-smi)."
        ),
    ),
}


def statevector_memory_bytes(num_qubits: int) -> int:
    """Exact bytes of the dense complex128 statevector array (16 B x 2^n)."""
    return 16 * (2**num_qubits)


def choose_simulation_method(circuit: QuantumCircuit) -> str:
    """Depth-driven Aer method rule — ``basis: rule``.

    Shallow circuits (depth <= :data:`SHALLOW_DEPTH_THRESHOLD`) prefer the
    tensor-network ``matrix_product_state`` method, whose memory scales with
    entanglement rather than width; deeper circuits fall back to the dense
    ``statevector`` method, where MPS bond dimensions would explode.

    This is one *signal* inside :func:`core.backends.selector.select_backend`;
    it is re-exported by :mod:`core.analysis` and :mod:`worker.simulator`.
    """
    method = "matrix_product_state" if circuit.depth() <= SHALLOW_DEPTH_THRESHOLD else "statevector"
    return method


def capabilities_matrix() -> dict[str, dict[str, Any]]:
    """The full backend capability matrix with live availability per backend.

    ``gpu_statevector`` availability is probed from the environment (opt-in
    flag + ``nvidia-smi``); every other backend is always available on this
    platform. JSON-ready — the analyzer embeds it in its report.
    """
    gpu_available, gpu_reason = detect_gpu()
    matrix: dict[str, dict[str, Any]] = {}
    for name, capabilities in CAPABILITIES.items():
        entry: dict[str, Any] = {
            "name": capabilities.name,
            "display_name": capabilities.display_name,
            "aer_method": capabilities.aer_method,
            "device": capabilities.device,
            "max_qubits": capabilities.max_qubits,
            "requires_clifford": capabilities.requires_clifford,
            "note": capabilities.note,
        }
        if capabilities.device == "gpu":
            entry["available"] = gpu_available
            entry["unavailable_reason"] = None if gpu_available else gpu_reason
        else:
            entry["available"] = True
            entry["unavailable_reason"] = None
        matrix[name] = entry
    return matrix


# --------------------------------------------------------------------------------------
# Clifford classification (shared by the selector and the stabilizer backend)
# --------------------------------------------------------------------------------------


def _is_clifford_angle(value: float) -> bool:
    """True when ``value`` is a multiple of pi/2 within tolerance."""
    half_turns = value / (math.pi / 2)
    return abs(half_turns - round(half_turns)) <= CLIFFORD_ANGLE_TOLERANCE


def clifford_refusal(circuit: QuantumCircuit) -> str | None:
    """Return a human-readable refusal reason, or ``None`` for Clifford circuits.

    Conservative by design: classically-conditioned operations, symbolic
    (unbound) parameters and control-flow constructs are refused even when a
    specific instantiation might be Clifford.
    """
    for index, instruction in enumerate(circuit.data):
        operation = instruction.operation
        name = operation.name

        if getattr(operation, "condition", None) is not None:
            return f"operation #{index} '{name}' is classically conditioned (not Clifford)"

        if name in CLIFFORD_GATES:
            continue

        if name in CLIFFORD_ROTATION_GATES:
            if len(operation.params) != 1:
                return f"operation #{index} '{name}' does not carry exactly one angle"
            angle = operation.params[0]
            if not isinstance(angle, (int, float)):
                return f"operation #{index} '{name}' has a symbolic (unbound) angle"
            if _is_clifford_angle(float(angle)):
                continue
            return (
                f"operation #{index} '{name}({float(angle):.6g})' is not a Clifford "
                "rotation (angle must be a multiple of pi/2)"
            )

        return f"operation #{index} '{name}' is not a Clifford gate"

    return None


def is_clifford(circuit: QuantumCircuit) -> bool:
    """Whether every operation in ``circuit`` belongs to the Clifford group."""
    return clifford_refusal(circuit) is None
