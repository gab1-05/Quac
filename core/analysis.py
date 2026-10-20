"""Pre-flight circuit analysis — exact metrics, labelled estimates, warnings.

This module is the single source of truth for what Quac knows about a circuit
*before* it is simulated. It is consumed by two planes:

* the API's synchronous ``POST /v1/quantum/analyze`` endpoint (dashboard
  studio, pre-flight reports), and
* the method-selection rule used by the worker at execution time
  (:func:`choose_simulation_method`), so the recommendation shown up-front is
  the same rule the worker applies.

Honesty contract
----------------
Every number this module reports carries a ``basis`` tag:

* ``exact``      — computed deterministically from the parsed instruction
                   stream (gate counts, depth, memory formula, proxies).
* ``estimated``  — a heuristic band (runtime, cost) with the formula and its
                   uncertainty stated in the accompanying ``note``.
* ``rule``       — the deterministic backend-selection rule, not a measurement.

Entanglement proxies are **structural**: they summarise the two-qubit
interaction graph (components, bridges, span, 2q depth). They are *not* an
entanglement measure and are documented as such in every response.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from qiskit import QuantumCircuit

from core.circuit import build_circuit

logger = logging.getLogger("quac.core.analysis")

#: Circuits at or below this gate depth use the MPS (tensor-network) backend.
#: Moved here from the worker so the analyzer and the worker share one rule.
SHALLOW_DEPTH_THRESHOLD: Final[int] = 40
#: Mirrors the API firewall's hard cap (defence in depth if called directly).
QUBIT_CAP: Final[int] = 28
#: Widths at or above this produce a NEAR_QUBIT_CAP warning.
NEAR_QUBIT_CAP: Final[int] = 26
#: complex128 amplitude size used by the exact statevector memory formula.
STATEVECTOR_BYTES_PER_AMPLITUDE: Final[int] = 16
#: Statevector working set above this size triggers a memory-pressure warning.
MEMORY_WARNING_BYTES: Final[int] = 1 << 30  # 1 GiB

#: Runtime heuristic (see :func:`_estimate_runtime_seconds`).
_RUNTIME_FIXED_BASE_SECONDS: Final[float] = 0.05
_RUNTIME_PER_DEPTH_LAYER_SECONDS: Final[float] = 0.001
_RUNTIME_STATEVECTOR_K: Final[float] = 2e-5
_RUNTIME_MPS_FACTOR: Final[float] = 0.5
_RUNTIME_SPREAD_LOW: Final[float] = 0.4
_RUNTIME_SPREAD_HIGH: Final[float] = 3.0
#: Cap the exponential scale so absurd direct calls cannot overflow floats.
_RUNTIME_QUBIT_SCALE_CAP: Final[int] = 60

#: AWS Lambda us-east-1 (x86, Linux) published price components used for the
#: cost band. Verify against https://aws.amazon.com/lambda/pricing/ before
#: relying on these numbers for billing — they are labelled estimated.
LAMBDA_GB_SECOND_USD: Final[float] = 0.0000166667
LAMBDA_REQUEST_USD: Final[float] = 0.0000002  # $0.20 per 1M requests
#: Matches the terraform default ``worker_memory_size`` (2048 MB).
WORKER_MEMORY_GB: Final[float] = 2.0

#: Self-inverse gates (G.G = I) — adjacent pairs cancel *exactly*.
_SELF_INVERSE_ONE_QUBIT: Final[frozenset[str]] = frozenset({"x", "y", "z", "h", "id"})
_SELF_INVERSE_TWO_QUBIT: Final[frozenset[str]] = frozenset({"cx", "cy", "cz", "swap", "ch"})

PROXY_NOTE: Final[str] = (
    "Structural proxies derived from the two-qubit interaction graph; they "
    "describe connectivity only — not entanglement. No state-based "
    "entanglement measure (entropy, concurrence, …) is computed."
)


def choose_simulation_method(circuit: QuantumCircuit) -> str:
    """Select the Aer simulation method from the circuit's structural shape.

    * Shallow circuits (depth <= :data:`SHALLOW_DEPTH_THRESHOLD`) are simulated
      with the tensor-network ``matrix_product_state`` method, which exploits
      limited entanglement spread and keeps memory sub-linear in qubit count.
    * Deeper, densely entangled circuits fall back to the dense ``statevector``
      method, where MPS bond dimensions would explode.
    """
    depth = circuit.depth()
    method = "matrix_product_state" if depth <= SHALLOW_DEPTH_THRESHOLD else "statevector"
    logger.debug(
        "method selection depth=%d threshold=%d -> %s",
        depth,
        SHALLOW_DEPTH_THRESHOLD,
        method,
    )
    return method


# --------------------------------------------------------------------------------------
# Structural metrics (exact)
# --------------------------------------------------------------------------------------


def _layered_operations(circuit: QuantumCircuit) -> list[tuple[int, str, list[int]]]:
    """ASAP-schedule every operation onto a layer index.

    Barriers are scheduling directives, not gates: they are excluded, which
    matches :meth:`qiskit.QuantumCircuit.depth` (verified: a barrier does not
    change the reported depth).

    Returns:
            ``(layer, gate_name, qubit_indices)`` per operation, in program
            order. Layer 0 is the first layer; the critical-path length is
            ``max(layer) + 1``.
    """
    last_layer: dict[int, int] = {}
    layered: list[tuple[int, str, list[int]]] = []
    for instruction in circuit.data:
        operation = instruction.operation
        if operation.name == "barrier":
            continue
        indices = [circuit.find_bit(q).index for q in instruction.qubits]
        layer = max((last_layer.get(index, -1) for index in indices), default=-1) + 1
        for index in indices:
            last_layer[index] = layer
        layered.append((layer, operation.name, indices))
    return layered


def _count_cancellations(layered: list[tuple[int, str, list[int]]]) -> int:
    """Count exact pairwise cancellations of adjacent self-inverse gates.

    A stack-based maximal matching: when two adjacent (barrier-separated-free)
    operations are the same self-inverse gate on the same qubit(s), they
    compose to the identity and are cancelled. Measurements and other gates
    break adjacency — cancelling across them would be unsound.
    """
    stack: list[tuple[str, tuple[int, ...]]] = []
    cancellations = 0
    for _, name, indices in layered:
        qubits = tuple(indices)
        self_inverse = (name in _SELF_INVERSE_ONE_QUBIT and len(qubits) == 1) or (
            name in _SELF_INVERSE_TWO_QUBIT and len(qubits) == 2
        )
        if self_inverse and stack and stack[-1] == (name, qubits):
            stack.pop()
            cancellations += 1
            continue
        stack.append((name, qubits))
    return cancellations


def _connected_without_edge(
    adjacency: dict[int, set[int]], source: int, target: int, removed: tuple[int, int]
) -> bool:
    """BFS reachability ignoring one specific edge (bridge test helper)."""
    seen = {source}
    queue = [source]
    while queue:
        node = queue.pop()
        for neighbour in adjacency[node]:
            edge = (min(node, neighbour), max(node, neighbour))
            if edge == removed or neighbour in seen:
                continue
            if neighbour == target:
                return True
            seen.add(neighbour)
            queue.append(neighbour)
    return False


def _interaction_proxies(
    layered: list[tuple[int, str, list[int]]], num_qubits: int
) -> dict[str, Any]:
    """Structural two-qubit interaction-graph proxies (exact).

    Graph model: nodes are qubit indices ``0..num_qubits-1``; every operation
    touching >= 2 qubits contributes an edge between each qubit pair it
    touches (a 3-qubit gate contributes its three pairs).
    """
    edge_counts: dict[tuple[int, int], int] = {}
    touching_two_or_more = 0
    for _, _, indices in layered:
        if len(indices) < 2:
            continue
        touching_two_or_more += 1
        for left in range(len(indices)):
            for right in range(left + 1, len(indices)):
                pair = (min(indices[left], indices[right]), max(indices[left], indices[right]))
                edge_counts[pair] = edge_counts.get(pair, 0) + 1

    edges = sorted(edge_counts)
    adjacency: dict[int, set[int]] = {index: set() for index in range(num_qubits)}
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)

    # Connected components over all qubit nodes (isolated qubits count as
    # size-1 components — width that never interacts is itself a signal).
    seen: set[int] = set()
    component_sizes: list[int] = []
    for start in range(num_qubits):
        if start in seen:
            continue
        size = 0
        queue = [start]
        seen.add(start)
        while queue:
            node = queue.pop()
            size += 1
            for neighbour in adjacency[node]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)
        component_sizes.append(size)
    component_sizes.sort(reverse=True)

    bridges = sum(
        1
        for pair in edges
        if not _connected_without_edge(adjacency, pair[0], pair[1], removed=pair)
    )

    # Two-qubit depth: ASAP layers counting only >=2q operations.
    last_layer: dict[int, int] = {}
    two_qubit_depth = 0
    for _, _, indices in layered:
        if len(indices) < 2:
            continue
        layer = max(last_layer.get(index, -1) for index in indices) + 1
        for index in indices:
            last_layer[index] = layer
        two_qubit_depth = max(two_qubit_depth, layer + 1)

    return {
        "edges": [[left, right] for left, right in edges],
        "edge_operations": touching_two_or_more,
        "components": len(component_sizes),
        "component_sizes": component_sizes,
        "max_component_qubits": component_sizes[0] if component_sizes else 0,
        "bridge_edges": bridges,
        "max_interaction_span": max((right - left for left, right in edges), default=0),
        "two_qubit_depth": two_qubit_depth,
        "note": PROXY_NOTE,
    }


# --------------------------------------------------------------------------------------
# Labelled estimates
# --------------------------------------------------------------------------------------


def _estimate_runtime_seconds(
    num_qubits: int, depth: int, method: str
) -> tuple[float, float]:
    """Heuristic runtime band in seconds — ``basis: estimated``.

    Point value ``t = fixed + per-layer + K * 2**qubits * (depth + 10) * f``
    with ``f = 0.5`` (MPS) or ``1.0`` (statevector); the band is ``[0.4t, 3t]``.
    Calibrated against local Aer CPU runs of reference circuits — this is a
    planning heuristic, not a benchmark.
    """
    factor = _RUNTIME_MPS_FACTOR if method == "matrix_product_state" else 1.0
    scale = float(2 ** min(num_qubits, _RUNTIME_QUBIT_SCALE_CAP))
    point = (
        _RUNTIME_FIXED_BASE_SECONDS
        + _RUNTIME_PER_DEPTH_LAYER_SECONDS * depth
        + _RUNTIME_STATEVECTOR_K * scale * (depth + 10) * factor
    )
    return round(max(point * _RUNTIME_SPREAD_LOW, 0.01), 3), round(
        point * _RUNTIME_SPREAD_HIGH, 3
    )


def _estimate_cost_usd(runtime_low: float, runtime_high: float) -> tuple[float, float]:
    """Cost band in USD — ``basis: estimated`` (Lambda GB-s pricing)."""
    per_second = WORKER_MEMORY_GB * LAMBDA_GB_SECOND_USD
    low = round(runtime_low * per_second + LAMBDA_REQUEST_USD, 8)
    high = round(runtime_high * per_second + LAMBDA_REQUEST_USD, 8)
    return low, high


# --------------------------------------------------------------------------------------
# Public analysis API
# --------------------------------------------------------------------------------------


def analyze_quantum_circuit(circuit: QuantumCircuit, shots: int) -> dict[str, Any]:
    """Compute the full pre-flight report for an already-built circuit.

    Args:
            circuit: Parsed circuit (the same object the worker would execute).
            shots: Requested sample count, used only by the runtime/cost bands.

    Returns:
            A JSON-ready report dictionary; see :func:`analyze` for the shape.
    """
    layered = _layered_operations(circuit)
    num_qubits = circuit.num_qubits

    histogram: dict[str, int] = {}
    one_qubit = two_qubit = three_plus_qubit = 0
    measurement_count = 0
    for _, name, indices in layered:
        histogram[name] = histogram.get(name, 0) + 1
        if name == "measure":
            measurement_count += 1
        elif len(indices) == 1:
            one_qubit += 1
        elif len(indices) == 2:
            two_qubit += 1
        else:
            three_plus_qubit += 1

    max_layer = max((layer for layer, _, _ in layered), default=-1)
    critical_length = max_layer + 1 if layered else 0
    path_ops = [entry for entry in layered if entry[0] == max_layer]
    critical_qubits = sorted({index for _, _, indices in path_ops for index in indices})
    critical_gates: dict[str, int] = {}
    for _, name, _ in path_ops:
        critical_gates[name] = critical_gates.get(name, 0) + 1

    total_gates = one_qubit + two_qubit + three_plus_qubit
    depth = int(circuit.depth())
    parameter_names = sorted(str(parameter) for parameter in circuit.parameters)
    active = {index for _, _, indices in layered for index in indices}
    unused_qubits = sum(1 for qubit in range(num_qubits) if qubit not in active)
    cancellations = _count_cancellations(layered)
    method = choose_simulation_method(circuit)

    statevector_bytes = STATEVECTOR_BYTES_PER_AMPLITUDE * (2**num_qubits)
    runtime_low, runtime_high = _estimate_runtime_seconds(num_qubits, depth, method)
    cost_low, cost_high = _estimate_cost_usd(runtime_low, runtime_high)

    warnings: list[dict[str, str]] = []
    if measurement_count == 0:
        warnings.append(
            {
                "code": "NO_MEASUREMENTS",
                "severity": "info",
                "message": (
                    "circuit contains no measure instructions; the worker measures "
                    "every qubit at the end, so result bitstrings are assumed to be "
                    "in the computational basis"
                ),
            }
        )
    if num_qubits >= NEAR_QUBIT_CAP:
        warnings.append(
            {
                "code": "NEAR_QUBIT_CAP",
                "severity": "warning",
                "message": (
                    f"{num_qubits} qubits is at or beyond the warning threshold "
                    f"({NEAR_QUBIT_CAP}) of the {QUBIT_CAP}-qubit cap; runtime and "
                    "memory grow exponentially with width"
                ),
            }
        )
    if parameter_names:
        warnings.append(
            {
                "code": "UNBOUND_PARAMETERS",
                "severity": "warning",
                "message": (
                    f"circuit has {len(parameter_names)} unbound parameter(s) "
                    f"({', '.join(parameter_names[:5])}); it cannot execute until "
                    "they are bound"
                ),
            }
        )
    if cancellations:
        warnings.append(
            {
                "code": "REDUNDANT_ADJACENT_GATES",
                "severity": "opportunity",
                "message": (
                    f"{cancellations} adjacent self-inverse gate pair(s) cancel "
                    "exactly; removing them preserves the output distribution and "
                    "reduces depth"
                ),
            }
        )
    if unused_qubits:
        warnings.append(
            {
                "code": "UNUSED_QUBITS",
                "severity": "opportunity",
                "message": (
                    f"{unused_qubits} of {num_qubits} qubits never participate in any "
                    "operation; a narrower register would cut memory and runtime"
                ),
            }
        )
    if depth > SHALLOW_DEPTH_THRESHOLD:
        warnings.append(
            {
                "code": "DEEP_CIRCUIT_STATEVECTOR",
                "severity": "info",
                "message": (
                    f"depth {depth} exceeds the MPS threshold "
                    f"({SHALLOW_DEPTH_THRESHOLD}); the dense statevector method "
                    "will be used and memory grows as 2^qubits"
                ),
            }
        )
    if statevector_bytes > MEMORY_WARNING_BYTES:
        warnings.append(
            {
                "code": "MEMORY_PRESSURE",
                "severity": "warning",
                "message": (
                    f"exact statevector array is {statevector_bytes / (1 << 30):.2f} GiB "
                    "(16 B x 2^qubits); transient workspace pushes the requirement higher"
                ),
            }
        )
    if num_qubits > QUBIT_CAP:
        warnings.append(
            {
                "code": "OVER_QUBIT_CAP",
                "severity": "warning",
                "message": (
                    f"{num_qubits} qubits exceeds the platform cap of {QUBIT_CAP}; "
                    "the API firewall rejects such circuits before analysis"
                ),
            }
        )

    reasons: list[str] = []
    if method == "matrix_product_state":
        reasons.append(
            f"depth {depth} <= {SHALLOW_DEPTH_THRESHOLD} -> matrix_product_state "
            "(tensor-network memory grows sub-linearly with qubit count)"
        )
    else:
        reasons.append(
            f"depth {depth} > {SHALLOW_DEPTH_THRESHOLD} -> statevector "
            "(MPS bond dimensions would explode on deep circuits)"
        )
    caveats = [w["message"] for w in warnings if w["severity"] == "warning"]

    logger.info(
        "analysis complete qubits=%d depth=%d shots=%d warnings=%d",
        num_qubits,
        depth,
        shots,
        len(warnings),
    )

    return {
        "shots": shots,
        "num_qubits": num_qubits,
        "num_clbits": int(circuit.num_clbits),
        "depth": depth,
        "total_operations": len(layered),
        "gate_histogram": histogram,
        "arity": {
            "one_qubit": one_qubit,
            "two_qubit": two_qubit,
            "three_plus_qubit": three_plus_qubit,
        },
        "measurement_count": measurement_count,
        "parameter_count": len(parameter_names),
        "unbound_parameter_names": parameter_names,
        "executable": not parameter_names,
        "two_qubit_density": round(two_qubit / max(total_gates, 1), 4),
        "critical_path": {
            "length": critical_length,
            "qubits": critical_qubits,
            "gates": critical_gates,
        },
        "interaction": _interaction_proxies(layered, num_qubits),
        "estimates": {
            "statevector_memory_bytes": {
                "value": statevector_bytes,
                "basis": "exact",
                "note": (
                    "16 bytes x 2^qubits (complex128 statevector array); excludes "
                    "transient transpiler/Aer workspace"
                ),
            },
            "runtime_seconds": {
                "low": runtime_low,
                "high": runtime_high,
                "basis": "estimated",
                "note": (
                    "log-linear heuristic t = 0.05 + 0.001*depth + "
                    "2e-5*2^qubits*(depth+10)*method-factor, band = [0.4t, 3t]; "
                    "calibrated against local Aer CPU runs — planning guidance, "
                    "not a benchmark"
                ),
            },
            "compute_cost_usd": {
                "low": cost_low,
                "high": cost_high,
                "basis": "estimated",
                "note": (
                    f"runtime band x {WORKER_MEMORY_GB} GB x "
                    f"${LAMBDA_GB_SECOND_USD}/GB-s + ${LAMBDA_REQUEST_USD}/request "
                    "(AWS Lambda us-east-1 x86 published prices; inherits the "
                    "runtime band's uncertainty — verify current AWS pricing)"
                ),
            },
            "recommended_method": {
                "value": method,
                "basis": "rule",
                "note": (
                    f"depth <= {SHALLOW_DEPTH_THRESHOLD} -> matrix_product_state, "
                    "else statevector"
                ),
            },
        },
        "warnings": warnings,
        "recommendation": {
            "method": method,
            "reasons": reasons,
            "caveats": caveats,
        },
    }


def analyze(circuit_data: str | dict[str, Any], circuit_format: str, shots: int) -> dict[str, Any]:
    """Parse a payload through the shared format factory and analyze it.

    The API layer must run its validation firewall *before* calling this; the
    function itself performs no I/O, mutates no state and touches no AWS
    service.

    Returns:
            JSON-ready report: structural metrics, interaction proxies,
            labelled estimates and warnings, plus a ``format`` tag.
    """
    circuit = build_circuit(circuit_data, circuit_format)
    report = analyze_quantum_circuit(circuit, shots)
    report["format"] = circuit_format
    return report


__all__ = [
    "SHALLOW_DEPTH_THRESHOLD",
    "analyze",
    "analyze_quantum_circuit",
    "choose_simulation_method",
]
