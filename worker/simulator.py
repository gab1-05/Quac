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
  / ``cirq_json`` payloads into a ``QuantumCircuit``.
* :func:`choose_simulation_method` — depth-driven backend selection
  (``matrix_product_state`` for shallow circuits, ``statevector`` for dense
  entanglements).
* :func:`run_simulation` — transpile + execute + sample counts, with guarded
  statevector probability extraction for small registers.
"""

from __future__ import annotations

import json
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

logger = logging.getLogger("quac.worker.simulator")

# --------------------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------------------

SHALLOW_DEPTH_THRESHOLD: Final[int] = 40
"""Circuits at or below this gate depth use the MPS (tensor-network) backend."""

MAX_STATEVECTOR_QUBITS: Final[int] = 16
"""Above this width probability extraction is skipped to bound memory growth."""

TRANSPILE_OPTIMIZATION_LEVEL: Final[int] = 2

#: Gate names accepted by the Cirq → Qiskit translator (allowlist semantics).
_CIRQ_TO_QISKIT_GATE_MAP: Final[dict[str, str]] = {
    "i": "id",
    "iden": "id",
    "x": "x",
    "y": "y",
    "z": "z",
    "h": "h",
    "s": "s",
    "sdg": "sdg",
    "t": "t",
    "tdg": "tdg",
    "sx": "sx",
    "rx": "rx",
    "ry": "ry",
    "rz": "rz",
    "u1": "u1",
    "u2": "u2",
    "u3": "u3",
    "cx": "cx",
    "cnot": "cx",
    "cy": "cy",
    "cz": "cz",
    "swap": "swap",
    "iswap": "iswap",
    "ch": "ch",
    "cp": "cp",
    "crx": "crx",
    "cry": "cry",
    "crz": "crz",
}


class SimulationBuildError(ValueError):
    """Raised when a payload cannot be converted into a ``QuantumCircuit``."""


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


# --------------------------------------------------------------------------------------
# Format factory
# --------------------------------------------------------------------------------------


def _circuit_from_qasm3(source: str) -> QuantumCircuit:
    """Parse an OpenQASM document with Qiskit.

    ``QuantumCircuit.from_qasm_str()`` is the reference entry point but it only
    accepts OpenQASM **2.0** sources. Documents declaring ``OPENQASM 3.x`` are
    routed through :func:`qiskit.qasm3.loads` (the Qiskit QASM 3 importer,
    provided by the ``qiskit-qasm3-import`` package); if that optional importer
    is unavailable the loader falls back to ``from_qasm_str()`` so QASM 2.0
    payloads keep working unchanged.
    """
    declares_qasm3 = source.lstrip().startswith("OPENQASM 3")
    if declares_qasm3:
        try:
            import qiskit.qasm3

            circuit = qiskit.qasm3.loads(source)
        except ImportError as exc:
            raise SimulationBuildError(
                "OpenQASM 3.0 source provided but the qiskit-qasm3-import "
                "package is missing; install worker/requirements.txt"
            ) from exc
        except Exception as exc:
            raise SimulationBuildError(f"OpenQASM 3.0 parse failure: {exc}") from exc
        if not isinstance(circuit, QuantumCircuit):  # pragma: no cover - defensive
            raise SimulationBuildError("qasm3 parser returned an unexpected object")
        return circuit

    try:
        circuit = QuantumCircuit.from_qasm_str(source)
    except Exception as exc:
        raise SimulationBuildError(f"OpenQASM parse failure: {exc}") from exc
    if not isinstance(circuit, QuantumCircuit):  # pragma: no cover - defensive
        raise SimulationBuildError("qasm parser returned an unexpected object")
    return circuit


def _circuit_from_qiskit_json(payload: dict[str, Any]) -> QuantumCircuit:
    """Build a circuit from a serialized Qiskit document.

    Three accepted shapes are handled, in order of precedence:

    1. ``{"qasm": "<openqasm source>"}`` — passthrough to the QASM parser.
    2. ``{"num_qubits": n, "instructions": [{"name", "qubits", "clbits", "params"}]}``
       — the generic Quac instruction interchange format.
    3. Anything :meth:`qiskit.QuantumCircuit.from_dict` natively understands
       (Qiskit's own dictionary projection), when available.
    """
    if "qasm" in payload and isinstance(payload["qasm"], str):
        return _circuit_from_qasm3(payload["qasm"])

    instructions = payload.get("instructions") or payload.get("operations")
    if isinstance(instructions, list):
        return _circuit_from_instructions(payload, instructions)

    native_loader = getattr(QuantumCircuit, "from_dict", None)
    if callable(native_loader):
        try:
            circuit = native_loader(payload)
        except Exception as exc:
            raise SimulationBuildError(f"qiskit_json parse failure: {exc}") from exc
        if isinstance(circuit, QuantumCircuit):
            return circuit

    raise SimulationBuildError(
        "qiskit_json payload must contain 'qasm' or an 'instructions' list"
    )


def _circuit_from_instructions(
    payload: dict[str, Any], instructions: list[Any]
) -> QuantumCircuit:
    """Materialise a circuit from the generic instruction interchange format."""
    num_qubits = int(
        payload.get("num_qubits") or payload.get("n_qubits") or payload.get("qubit_count") or 0
    )
    if num_qubits <= 0:
        widest_index = 0
        for op in instructions:
            if isinstance(op, dict):
                for qubit in op.get("qubits", []) or []:
                    widest_index = max(widest_index, int(qubit))
        num_qubits = widest_index + 1
    num_clbits = int(payload.get("num_clbits") or 0)
    if num_clbits <= 0:
        measured = [
            max(op.get("clbits", [0]), default=0)
            for op in instructions
            if isinstance(op, dict) and op.get("name") in ("measure", "measure_all")
        ]
        num_clbits = (max(measured) + 1) if measured else 0

    circuit = QuantumCircuit(num_qubits, num_clbits) if num_clbits else QuantumCircuit(num_qubits)

    for index, op in enumerate(instructions):
        if not isinstance(op, dict) or not isinstance(op.get("name"), str):
            raise SimulationBuildError(f"instruction at index {index} must have a string 'name'")
        name = op["name"].lower()
        qubits = [int(q) for q in op.get("qubits", [])]
        clbits = [int(c) for c in op.get("clbits", [])]
        params = [float(p) for p in op.get("params", [])]

        try:
            if name in ("measure", "measure_all"):
                if name == "measure_all":
                    circuit.measure_all()
                else:
                    if len(qubits) != 1 or len(clbits) != 1:
                        raise SimulationBuildError(
                            f"instruction 'measure' requires 1 qubit and 1 clbit (index {index})"
                        )
                    circuit.measure(qubits[0], clbits[0])
                continue
            if name in ("barrier", "reset", "id"):
                if not qubits:
                    raise SimulationBuildError(
                        f"instruction {name!r} has no target qubits (index {index})"
                    )
                getattr(circuit, name)(*qubits)
                continue
            if name == "delay":
                if not qubits or not params:
                    raise SimulationBuildError(
                        f"instruction 'delay' requires a duration and target qubits (index {index})"
                    )
                circuit.delay(params[0], *qubits)
                continue

            method = getattr(circuit, name, None)
            if method is None:
                raise SimulationBuildError(f"unsupported gate {name!r} at index {index}")
            # Qiskit gate methods take rotation parameters first, then targets.
            bound_args = [*params, *qubits]
            if not qubits:
                raise SimulationBuildError(f"gate {name!r} at index {index} has no target qubits")
            method(*bound_args)
        except SimulationBuildError:
            raise
        except Exception as exc:
            raise SimulationBuildError(
                f"failed to apply instruction {name!r} at index {index}: {exc}"
            ) from exc

    return circuit


def _circuit_from_cirq_json(payload: dict[str, Any]) -> QuantumCircuit:
    """Deserialize a Cirq JSON document and translate it into Qiskit.

    ``cirq-core`` is imported lazily here: it is a comparatively large pure
    Python package, and keeping it out of module scope avoids paying its
    import cost on environments that only execute qasm3/qiskit payloads.
    The heavy *compiled* packages (qiskit / qiskit-aer / numpy) remain at
    module scope for cold-start purposes.
    """
    try:
        import cirq
    except ImportError as exc:
        raise SimulationBuildError(
            "cirq-core is not installed in this worker image; "
            "install worker/requirements.txt to enable cirq_json payloads"
        ) from exc

    try:
        if "circuit" in payload:
            document = payload["circuit"]
            circuit_text = document if isinstance(document, str) else None
            cirq_circuit = (
                cirq.read_json(json_text=circuit_text)
                if circuit_text
                else cirq.read_json(json_text=json.dumps(document))
            )
        else:
            cirq_circuit = cirq.read_json(json_text=json.dumps(payload))
    except Exception as exc:
        raise SimulationBuildError(f"cirq_json parse failure: {exc}") from exc

    # cirq.read_json silently passes through JSON that carries no cirq_type
    # (e.g. a bare dict); fail with the typed error instead of an opaque
    # AttributeError deep in translation.
    if not isinstance(cirq_circuit, cirq.Circuit):
        raise SimulationBuildError(
            "cirq_json payload did not deserialize to a cirq.Circuit"
        )

    return _translate_cirq_circuit(cirq_circuit)


def _translate_cirq_circuit(cirq_circuit: Any) -> QuantumCircuit:
    """Translate a ``cirq.Circuit`` gate-by-gate against a strict allowlist."""
    try:
        import cirq
    except ImportError as exc:  # pragma: no cover - guarded by caller
        raise SimulationBuildError("cirq-core unavailable") from exc

    ordered_qubits = sorted(cirq_circuit.all_qubits(), key=str)
    qubit_index = {q: i for i, q in enumerate(ordered_qubits)}
    num_qubits = len(ordered_qubits)
    if num_qubits == 0:
        raise SimulationBuildError("cirq circuit contains no qubits")

    has_explicit_measure = any(
        cirq.is_measurement(op) for moment in cirq_circuit for op in moment.operations
    )
    num_clbits = num_qubits if has_explicit_measure else 0
    qiskit_circuit = (
        QuantumCircuit(num_qubits, num_clbits) if num_clbits else QuantumCircuit(num_qubits)
    )

    next_clbit = 0
    for moment in cirq_circuit:
        for operation in moment.operations:
            indices = [qubit_index[q] for q in operation.qubits]

            if cirq.is_measurement(operation):
                for position, qubit_position in enumerate(indices):
                    if not num_clbits:
                        raise SimulationBuildError("measurement found without classical register")
                    qiskit_circuit.measure(qubit_position, (next_clbit + position) % num_clbits)
                next_clbit += len(indices)
                continue

            gate = operation.gate
            gate_name = type(gate).__name__.lower()
            qiskit_name = _CIRQ_TO_QISKIT_GATE_MAP.get(gate_name)
            if qiskit_name is None:
                raise SimulationBuildError(
                    f"unsupported cirq gate {type(gate).__name__!r}; "
                    f"allowed: {sorted(set(_CIRQ_TO_QISKIT_GATE_MAP))}"
                )
            params = _cirq_gate_angles(gate)
            if gate_name in ("rx", "ry", "rz") and len(params) != 1:
                raise SimulationBuildError(
                    f"cirq rotation gate {type(gate).__name__!r} requires exactly one angle"
                )
            _apply_gate(qiskit_circuit, qiskit_name, indices, params)

    if not any(qiskit_circuit.data):
        raise SimulationBuildError("cirq circuit contains no operations")
    return qiskit_circuit


def _cirq_gate_angles(gate: Any) -> list[float]:
    """Extract rotation angles from a Cirq gate, normalised to radians."""
    half_turns = getattr(gate, "half_turns", None)
    if isinstance(half_turns, (int, float)):
        return [math.pi * float(half_turns)]
    rads = getattr(gate, "rads", None)
    if isinstance(rads, (int, float)):
        return [float(rads)]
    return []


def _apply_gate(
    circuit: QuantumCircuit, name: str, indices: list[int], params: list[float]
) -> None:
    """Apply a single allowlisted gate onto the Qiskit circuit."""
    method = getattr(circuit, name, None)
    if method is None:
        raise SimulationBuildError(f"unsupported gate {name!r}")
    try:
        if params:
            method(*params, *indices)
        else:
            method(*indices)
    except Exception as exc:
        raise SimulationBuildError(f"failed to apply gate {name!r}: {exc}") from exc


def build_circuit(circuit_data: str | dict[str, Any], circuit_format: str) -> QuantumCircuit:
    """Convert a raw payload into a ``QuantumCircuit`` according to ``circuit_format``.

    Args:
            circuit_data: Raw payload (QASM source string or JSON dictionary).
            circuit_format: One of ``qasm3``, ``qiskit_json``, ``cirq_json``.

    Returns:
            A parsed :class:`~qiskit.QuantumCircuit`.

    Raises:
            SimulationBuildError: on any parse or translation failure.
    """
    fmt = str(circuit_format).lower()
    logger.debug("building circuit format=%s bytes=%d", fmt, len(str(circuit_data)))

    if fmt == "qasm3":
        if not isinstance(circuit_data, str):
            raise SimulationBuildError("qasm3 payloads must be strings")
        return _circuit_from_qasm3(circuit_data)

    if fmt == "qiskit_json":
        if isinstance(circuit_data, str):
            try:
                circuit_data = json.loads(circuit_data)
            except json.JSONDecodeError as exc:
                raise SimulationBuildError(f"qiskit_json is not valid JSON: {exc}") from exc
        if not isinstance(circuit_data, dict):
            raise SimulationBuildError("qiskit_json payloads must be JSON objects")
        return _circuit_from_qiskit_json(circuit_data)

    if fmt == "cirq_json":
        if isinstance(circuit_data, str):
            try:
                circuit_data = json.loads(circuit_data)
            except json.JSONDecodeError as exc:
                raise SimulationBuildError(f"cirq_json is not valid JSON: {exc}") from exc
        if not isinstance(circuit_data, dict):
            raise SimulationBuildError("cirq_json payloads must be JSON objects")
        return _circuit_from_cirq_json(circuit_data)

    raise SimulationBuildError(f"unsupported circuit format {circuit_format!r}")


# --------------------------------------------------------------------------------------
# Backend method selection
# --------------------------------------------------------------------------------------


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
