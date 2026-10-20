"""Backend capability model, selector and Aer implementations.

Layering rule
-------------
* :mod:`core.backends.base` — pure metadata/classification (importable by
  both the API and the worker planes).
* :mod:`core.backends.selector` — deterministic selection logic (pure).
* :mod:`core.backends.classical` — Aer simulator implementations; **only the
  worker imports this** (the API image does not ship ``qiskit-aer``).
"""

from core.backends.base import (
    CAPABILITIES,
    CLIFFORD_ANGLE_TOLERANCE,
    CLIFFORD_GATES,
    CLIFFORD_ROTATION_GATES,
    QUBIT_CAP,
    SHALLOW_DEPTH_THRESHOLD,
    STATEVECTOR_MAX_QUBITS,
    BackendCapabilities,
    CapacitySignals,
    capabilities_matrix,
    choose_simulation_method,
    clifford_refusal,
    detect_gpu,
    is_clifford,
    statevector_memory_bytes,
    worker_memory_budget_mb,
)
from core.backends.selector import (
    BACKEND_NAMES,
    DEFAULT_MEMORY_BUDGET_MB,
    STATEVECTOR_BUDGET_SHARE,
    BackendSelection,
    BackendUnavailableError,
    select_backend,
)

__all__ = [
    "BACKEND_NAMES",
    "CAPABILITIES",
    "CLIFFORD_ANGLE_TOLERANCE",
    "CLIFFORD_GATES",
    "CLIFFORD_ROTATION_GATES",
    "DEFAULT_MEMORY_BUDGET_MB",
    "QUBIT_CAP",
    "SHALLOW_DEPTH_THRESHOLD",
    "STATEVECTOR_BUDGET_SHARE",
    "STATEVECTOR_MAX_QUBITS",
    "BackendCapabilities",
    "BackendSelection",
    "BackendUnavailableError",
    "CapacitySignals",
    "capabilities_matrix",
    "choose_simulation_method",
    "clifford_refusal",
    "detect_gpu",
    "is_clifford",
    "select_backend",
    "statevector_memory_bytes",
    "worker_memory_budget_mb",
]
