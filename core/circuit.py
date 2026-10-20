"""Format factory — payload → :class:`~qiskit.QuantumCircuit`.

Single source of truth for circuit construction. Callers:

* the API's pre-flight analyzer (``POST /v1/quantum/analyze``),
* the worker's execution path (``worker.simulator.run_simulation``).

Keeping one implementation guarantees that the metrics shown before submission
are computed from exactly the circuit the worker would execute.

Accepted formats
----------------
* ``qasm3``       — OpenQASM 3.x (or 2.0) source string,
* ``qiskit_json`` — ``{"qasm": …}``, the generic instruction interchange
  format, or Qiskit's native circuit dict,
* ``cirq_json``   — a Cirq JSON document (``cirq-core`` imported lazily).

Heavy imports: ``qiskit`` at module scope (both planes already depend on it at
startup); ``cirq`` stays lazy because it is pure-Python and comparatively
large — environments without it fail with a typed error instead of an import
crash.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any, Final

from qiskit import QuantumCircuit

logger = logging.getLogger("quac.core.circuit")

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

#: Concrete cirq 1.x gate **class** names → Qiskit gate names.
#:
#: ``cirq.to_json()`` records gates by class name (``cirq_type``), and
#: ``type(gate).__name__`` on the deserialised object therefore yields names
#: like ``HPowGate`` / ``_PauliX`` / ``ZPowGate`` — never the lower-cased
#: short names in the legacy map above. Key facts encoded here:
#:
#: * ``ZPowGate`` covers Z **and** S/T/Sdg/Tdg (any exponent) — routed to
#:   ``rz`` at ``pi * exponent``, which is exact up to global phase
#:   (measurement statistics are identical);
#: * ``XPowGate``/``YPowGate`` are powers of X/Y and route to ``rx``/``ry``
#:   the same way; the constant singletons are the private ``_PauliX/Y/Z``
#:   classes (always exponent 1);
#: * rotations are classes literally named ``Rx``/``Ry``/``Rz``;
#: * ``IdentityGate`` normalises to ``id``.
_CIRQ_CLASS_TO_QISKIT_GATE: Final[dict[str, str]] = {
    "identitygate": "id",
    "hpowgate": "h",
    "_paulix": "x",
    "_pauliy": "y",
    "_pauliz": "z",
    "xpowgate": "rx",
    "ypowgate": "ry",
    "zpowgate": "rz",
    "rx": "rx",
    "ry": "ry",
    "rz": "rz",
    "sxgate": "sx",
    "sxdggate": "sxdg",
    "cxpowgate": "cx",
    "czpowgate": "cz",
    "swappowgate": "swap",
    "iswappowgate": "iswap",
}

#: Angle-carrying gates: ``pi * exponent`` (or ``rads``) is the angle, any
#: value is a well-defined translation.
_CIRQ_ANGLE_GATES: Final[frozenset[str]] = frozenset(
    {"rx", "ry", "rz", "u1", "u2", "u3", "cp", "crx", "cry", "crz"}
)

#: Fixed-matrix gates that equal themselves only at exponent +/-1 (Hermitian
#: involutions). ``H**0.5`` etc. has no single allowlisted equivalent and must
#: be rejected instead of silently translated as a plain ``h``.
_CIRQ_INVOLUTIVE_GATES: Final[frozenset[str]] = frozenset(
    {"id", "x", "y", "z", "h", "cx", "cy", "cz", "ch", "swap"}
)

#: Fixed-matrix gates valid only at exponent 1 — their inverse differs from
#: themselves (``Sdg ≠ S``, ``ISWAP† ≠ ISWAP``, ``SXdg ≠ SX``).
_CIRQ_EXACT_ONE_GATES: Final[frozenset[str]] = frozenset(
    {"s", "sdg", "t", "tdg", "sx", "sxdg", "iswap"}
)


class CircuitBuildError(ValueError):
    """Raised when a payload cannot be converted into a ``QuantumCircuit``."""


# --------------------------------------------------------------------------------------
# QASM
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
            raise CircuitBuildError(
                "OpenQASM 3.0 source provided but the qiskit-qasm3-import "
                "package is missing; install the platform requirements"
            ) from exc
        except Exception as exc:
            raise CircuitBuildError(f"OpenQASM 3.0 parse failure: {exc}") from exc
        if not isinstance(circuit, QuantumCircuit):  # pragma: no cover - defensive
            raise CircuitBuildError("qasm3 parser returned an unexpected object")
        return circuit

    try:
        circuit = QuantumCircuit.from_qasm_str(source)
    except Exception as exc:
        raise CircuitBuildError(f"OpenQASM parse failure: {exc}") from exc
    if not isinstance(circuit, QuantumCircuit):  # pragma: no cover - defensive
        raise CircuitBuildError("qasm parser returned an unexpected object")
    return circuit


# --------------------------------------------------------------------------------------
# Qiskit JSON
# --------------------------------------------------------------------------------------


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
            raise CircuitBuildError(f"qiskit_json parse failure: {exc}") from exc
        if isinstance(circuit, QuantumCircuit):
            return circuit

    raise CircuitBuildError(
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
            raise CircuitBuildError(f"instruction at index {index} must have a string 'name'")
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
                        raise CircuitBuildError(
                            f"instruction 'measure' requires 1 qubit and 1 clbit (index {index})"
                        )
                    circuit.measure(qubits[0], clbits[0])
                continue
            if name in ("barrier", "reset", "id"):
                if not qubits:
                    raise CircuitBuildError(
                        f"instruction {name!r} has no target qubits (index {index})"
                    )
                getattr(circuit, name)(*qubits)
                continue
            if name == "delay":
                if not qubits or not params:
                    raise CircuitBuildError(
                        f"instruction 'delay' requires a duration and target qubits (index {index})"
                    )
                circuit.delay(params[0], *qubits)
                continue

            method = getattr(circuit, name, None)
            if method is None:
                raise CircuitBuildError(f"unsupported gate {name!r} at index {index}")
            # Qiskit gate methods take rotation parameters first, then targets.
            bound_args = [*params, *qubits]
            if not qubits:
                raise CircuitBuildError(f"gate {name!r} at index {index} has no target qubits")
            method(*bound_args)
        except CircuitBuildError:
            raise
        except Exception as exc:
            raise CircuitBuildError(
                f"failed to apply instruction {name!r} at index {index}: {exc}"
            ) from exc

    return circuit


# --------------------------------------------------------------------------------------
# Cirq JSON
# --------------------------------------------------------------------------------------


def _circuit_from_cirq_json(payload: dict[str, Any]) -> QuantumCircuit:
    """Deserialize a Cirq JSON document and translate it into Qiskit.

    ``cirq-core`` is imported lazily here: it is a comparatively large pure
    Python package, and keeping it out of module scope avoids paying its
    import cost on environments that only execute qasm3/qiskit payloads.
    """
    try:
        import cirq
    except ImportError as exc:
        raise CircuitBuildError(
            "cirq-core is not installed in this environment; "
            "install the platform requirements to enable cirq_json payloads"
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
        raise CircuitBuildError(f"cirq_json parse failure: {exc}") from exc

    # cirq.read_json silently passes through JSON that carries no cirq_type
    # (e.g. a bare dict); fail with the typed error instead of an opaque
    # AttributeError deep in translation.
    if not isinstance(cirq_circuit, cirq.Circuit):
        raise CircuitBuildError(
            "cirq_json payload did not deserialize to a cirq.Circuit"
        )

    return _translate_cirq_circuit(cirq_circuit)


def _translate_cirq_circuit(cirq_circuit: Any) -> QuantumCircuit:
    """Translate a ``cirq.Circuit`` gate-by-gate against a strict allowlist."""
    try:
        import cirq
    except ImportError as exc:  # pragma: no cover - guarded by caller
        raise CircuitBuildError("cirq-core unavailable") from exc

    ordered_qubits = sorted(cirq_circuit.all_qubits(), key=str)
    qubit_index = {q: i for i, q in enumerate(ordered_qubits)}
    num_qubits = len(ordered_qubits)
    if num_qubits == 0:
        raise CircuitBuildError("cirq circuit contains no qubits")

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
                        raise CircuitBuildError("measurement found without classical register")
                    qiskit_circuit.measure(qubit_position, (next_clbit + position) % num_clbits)
                next_clbit += len(indices)
                continue

            gate = getattr(operation, "gate", None)
            if gate is None:
                # Composite operations (CircuitOperation, annotations) carry
                # no single gate to translate.
                raise CircuitBuildError(
                    f"unsupported cirq operation {type(operation).__name__!r} "
                    "(no gate to translate)"
                )
            gate_name = type(gate).__name__.lower()
            qiskit_name = _CIRQ_CLASS_TO_QISKIT_GATE.get(gate_name) or (
                _CIRQ_TO_QISKIT_GATE_MAP.get(gate_name)
            )
            if qiskit_name is None:
                raise CircuitBuildError(
                    f"unsupported cirq gate {type(gate).__name__!r}; allowed: "
                    f"{sorted(set(_CIRQ_CLASS_TO_QISKIT_GATE) | set(_CIRQ_TO_QISKIT_GATE_MAP))}"
                )
            if qiskit_name in _CIRQ_ANGLE_GATES:
                params = _cirq_gate_angles(gate)
                if not params:
                    raise CircuitBuildError(
                        f"cirq gate {type(gate).__name__!r} carries no numeric angle"
                    )
            else:
                # Fixed-matrix gate: validate the power, then drop the angle —
                # X/H/CNOT applied with a parameter would fail in Qiskit.
                params = []
                exponent = getattr(gate, "exponent", None)
                if isinstance(exponent, (int, float)):
                    power = float(exponent)
                    allowed = (
                        (1.0,) if qiskit_name in _CIRQ_EXACT_ONE_GATES else (1.0, -1.0)
                    )
                    if power not in allowed:
                        raise CircuitBuildError(
                            f"cirq gate {type(gate).__name__!r} with exponent "
                            f"{power!r} has no exact Qiskit equivalent; "
                            f"{qiskit_name!r} is translatable only at exponent "
                            f"{'/'.join(format(a, 'g') for a in allowed)}"
                        )
            _apply_gate(qiskit_circuit, qiskit_name, indices, params)

    if len(qiskit_circuit.data) == 0:
        raise CircuitBuildError("cirq circuit contains no operations")
    return qiskit_circuit


def _cirq_gate_angles(gate: Any) -> list[float]:
    """Extract the rotation angle (radians) from a Cirq gate.

    ``exponent`` is the universal cirq 1.x power attribute — the angle is
    ``pi * exponent``. For ``Rx``/``Ry``/``Rz`` the exponent is ``rads / pi``,
    so the conversion is exact. ``half_turns`` (pre-1.0 name) and ``rads``
    are kept as fallbacks; neither exists on every release, and gates
    carrying only a symbolic exponent are reported as having no numeric
    angle by the caller.
    """
    for attribute, scale in (("exponent", math.pi), ("half_turns", math.pi), ("rads", 1.0)):
        value = getattr(gate, attribute, None)
        if isinstance(value, (int, float)):
            return [scale * float(value)]
    return []


def _apply_gate(
    circuit: QuantumCircuit, name: str, indices: list[int], params: list[float]
) -> None:
    """Apply a single allowlisted gate onto the Qiskit circuit."""
    method = getattr(circuit, name, None)
    if method is None:
        raise CircuitBuildError(f"unsupported gate {name!r}")
    try:
        if params:
            method(*params, *indices)
        else:
            method(*indices)
    except Exception as exc:
        raise CircuitBuildError(f"failed to apply gate {name!r}: {exc}") from exc


# --------------------------------------------------------------------------------------
# Public factory
# --------------------------------------------------------------------------------------


def build_circuit(circuit_data: str | dict[str, Any], circuit_format: str) -> QuantumCircuit:
    """Convert a raw payload into a ``QuantumCircuit`` according to ``circuit_format``.

    Args:
            circuit_data: Raw payload (QASM source string or JSON dictionary).
            circuit_format: One of ``qasm3``, ``qiskit_json``, ``cirq_json``.

    Returns:
            A parsed :class:`~qiskit.QuantumCircuit`.

    Raises:
            CircuitBuildError: on any parse or translation failure.
    """
    fmt = str(circuit_format).lower()
    logger.debug("building circuit format=%s bytes=%d", fmt, len(str(circuit_data)))

    if fmt == "qasm3":
        if not isinstance(circuit_data, str):
            raise CircuitBuildError("qasm3 payloads must be strings")
        return _circuit_from_qasm3(circuit_data)

    if fmt == "qiskit_json":
        if isinstance(circuit_data, str):
            try:
                circuit_data = json.loads(circuit_data)
            except json.JSONDecodeError as exc:
                raise CircuitBuildError(f"qiskit_json is not valid JSON: {exc}") from exc
        if not isinstance(circuit_data, dict):
            raise CircuitBuildError("qiskit_json payloads must be JSON objects")
        return _circuit_from_qiskit_json(circuit_data)

    if fmt == "cirq_json":
        if isinstance(circuit_data, str):
            try:
                circuit_data = json.loads(circuit_data)
            except json.JSONDecodeError as exc:
                raise CircuitBuildError(f"cirq_json is not valid JSON: {exc}") from exc
        if not isinstance(circuit_data, dict):
            raise CircuitBuildError("cirq_json payloads must be JSON objects")
        return _circuit_from_cirq_json(circuit_data)

    raise CircuitBuildError(f"unsupported circuit format {circuit_format!r}")


__all__ = ["CircuitBuildError", "build_circuit"]
