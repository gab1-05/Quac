"""Quac API contracts and circuit validation firewall.

This module owns two responsibilities:

1. The Pydantic v2 wire contracts exchanged between Quac consumers and the
   ingestion API (``SimulationRequest`` / ``JobStatusResponse``).
2. The *circuit validation firewall*: a defense-in-depth static analysis
   stage that inspects raw circuit payloads **before** they are persisted or
   enqueued. No payload reaches SQS or the compute plane unless it passes
   every rule defined here.

Firewall rules implemented
--------------------------
* Hard payload size and line-count ceilings (resource-exhaustion guard).
* Comment and string-literal stripping before structural analysis so that
  comment-borne injection vectors cannot smuggle instructions past the
  parser.
* An allowlist regex grammar for OpenQASM 3.0. Every statement must match a
  sanctioned construct; anything else (``include <...>``, ``opaque``,
  ``def``/``function``, unbounded control flow, ``pragma``, file I/O hints)
  is rejected with the offending line number.
* Qubit-count extraction across all three supported formats with a hard
  infrastructure-cost ceiling of 28 qubits.
* JSON structural validation for ``qiskit_json`` / ``cirq_json`` payloads
  including nesting-depth limits and key allowlists.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Final

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------------------
# Global firewall tunables
# --------------------------------------------------------------------------------------

MAX_CIRCUIT_BYTES: Final[int] = 64 * 1024
"""Maximum accepted serialized circuit payload size (64 KiB)."""

MAX_CIRCUIT_LINES: Final[int] = 5_000
"""Maximum accepted line count for textual circuit payloads."""

MAX_QUBITS: Final[int] = 28
"""Infrastructure cost firewall: circuits wider than this are rejected at
ingest time so that runaway statevector allocations never reach the compute
plane (2**28 complex128 ~= 4 GiB for a single statevector amplitude set)."""

MAX_JSON_NESTING: Final[int] = 12
"""Maximum structural nesting depth accepted for JSON circuit payloads."""

MAX_IDEMPOTENCY_KEY_CHARS: Final[int] = 128
"""SQS FIFO ``MessageDeduplicationId`` length ceiling."""


# --------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------


class CircuitFormat(StrEnum):
    """Supported circuit serialization formats accepted by Quac."""

    QASM3 = "qasm3"
    QISKIT_JSON = "qiskit_json"
    CIRQ_JSON = "cirq_json"


class JobStatus(StrEnum):
    """Lifecycle states of a Quac simulation job."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


# --------------------------------------------------------------------------------------
# Firewall exceptions
# --------------------------------------------------------------------------------------


class CircuitValidationError(ValueError):
    """Raised when a circuit payload violates one or more firewall rules."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "CIRCUIT_REJECTED",
        line: int | None = None,
    ) -> None:
        self.code = code
        self.line = line
        self.detail = message
        rendered = message if line is None else f"{message} (line {line})"
        super().__init__(rendered)


class QubitLimitExceededError(CircuitValidationError):
    """Raised when the declared qubit width exceeds :data:`MAX_QUBITS`."""

    def __init__(self, observed: int, limit: int = MAX_QUBITS) -> None:
        self.observed = observed
        self.limit = limit
        super().__init__(
            f"circuit declares {observed} qubit(s); the platform limit is {limit} qubit(s)",
            code="QUBIT_LIMIT_EXCEEDED",
        )


# --------------------------------------------------------------------------------------
# Pydantic v2 wire contracts
# --------------------------------------------------------------------------------------


class SimulationRequest(BaseModel):
    """Ingestion contract for ``POST /v1/quantum/simulate``."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "circuit_data": (
                    "OPENQASM 3.0;\n"
                    "include \"stdgates.inc\";\n"
                    "qubit[2] q;\n"
                    "bit[2] c;\n"
                    "h q[0];\n"
                    "cx q[0], q[1];\n"
                    "c = measure q;\n"
                ),
                "format": "qasm3",
                "shots": 1024,
                "webhook_url": "https://consumer.example.com/hooks/quac",
                "idempotency_key": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
            }
        },
    )

    circuit_data: str | dict[str, Any] = Field(
        ...,
        description=(
            "Raw circuit payload: an OpenQASM 3.0 source string, or a dictionary "
            "structure for serialized Qiskit / Cirq documents."
        ),
    )
    format: CircuitFormat = Field(
        ...,
        description="Serialization format of ``circuit_data``.",
    )
    shots: int = Field(
        default=1024,
        ge=1,
        le=8192,
        description="Number of measurement shots to sample.",
    )
    webhook_url: AnyHttpUrl = Field(
        ...,
        description="Strictly validated HTTP/HTTPS callback URL for result delivery.",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=MAX_IDEMPOTENCY_KEY_CHARS,
        description=(
            "Optional client-supplied deduplication token. When present it is used "
            "verbatim (hashed) as the SQS FIFO MessageDeduplicationId."
        ),
    )

    @field_validator("webhook_url")
    @classmethod
    def _reject_credentials_in_webhook(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Reject ``user:pass@host`` webhook URLs (credential exfiltration guard)."""
        if value.username or value.password:
            raise ValueError("webhook_url must not embed credentials")
        if value.scheme not in ("http", "https"):
            raise ValueError("webhook_url must use http or https")
        return value

    @field_validator("circuit_data")
    @classmethod
    def _reject_oversized_payload(cls, value: str | dict[str, Any]) -> str | dict[str, Any]:
        """Enforce the hard byte ceiling before any downstream processing."""
        serialized = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > MAX_CIRCUIT_BYTES:
            raise ValueError(
                f"circuit_data exceeds the {MAX_CIRCUIT_BYTES} byte platform ceiling"
            )
        return value


class JobStatusResponse(BaseModel):
    """Tracking contract returned by ``GET /v1/quantum/jobs/{job_id}``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "job_id": "3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c",
                "status": "completed",
                "created_at": "2026-10-19T12:00:00+00:00",
                "completed_at": "2026-10-19T12:00:07+00:00",
                "error_message": None,
            }
        },
    )

    job_id: str = Field(..., description="UUIDv4 identifier of the simulation job.")
    status: JobStatus = Field(..., description="Current lifecycle state.")
    created_at: str = Field(..., description="RFC 3339 creation timestamp (UTC).")
    completed_at: str | None = Field(
        default=None,
        description="RFC 3339 completion timestamp (UTC); null until terminal state.",
    )
    error_message: str | None = Field(
        default=None,
        description="Failure detail or traceback summary when status == failed.",
    )


class SimulationAcceptedResponse(BaseModel):
    """202 Accepted body returned on successful enqueue."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    status_url: str
    detail: str = "simulation accepted for asynchronous execution"


# --------------------------------------------------------------------------------------
# OpenQASM 3.0 allowlist firewall
# --------------------------------------------------------------------------------------

_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_RE: Final[re.Pattern[str]] = re.compile(r'"(?:[^"\\]|\\.)*"')

_QUBIT_DECL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*qubit\s*(?:\[\s*(\d+)\s*\])?\s+[A-Za-z_][A-Za-z0-9_]*\s*;\s*$"
)
_QREG_DECL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*qreg\s+[A-Za-z_][A-Za-z0-9_]*\s*\[\s*(\d+)\s*\]\s*;\s*$"
)
_CREG_DECL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:bit|creg)\s*(?:\[\s*(\d+)\s*\])?\s*[A-Za-z_][A-Za-z0-9_]*\s*(?:\[\s*\d+\s*\])?\s*;\s*$"
)
_VERSION_PRAGMA_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*OPENQASM\s+(?:2\.0|3\.0(?:\.\d+)?)\s*;\s*$"
)
_INCLUDE_RE: Final[re.Pattern[str]] = re.compile(r'^\s*include\s+"[^"]*"\s*;\s*$')

_GATE_APP_RE: Final[re.Pattern[str]] = re.compile(
    # captures the gate name (group 1); optional register index and params,
    # then one or more qubit targets.
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*"
    r"(?:\([^;)]*\)\s*)?"
    r"[A-Za-z_][A-Za-z0-9_\[\],\s.]*\s*;\s*$"
)
_MEASURE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:c\s*=\s*)?measure\s+[A-Za-z_][A-Za-z0-9_\[\],\s]*;\s*$"
)
_BARRIER_RE: Final[re.Pattern[str]] = re.compile(r"^\s*barrier\b[^;]*;\s*$")
_RESET_RE: Final[re.Pattern[str]] = re.compile(r"^\s*reset\s+[^;]+;\s*$")
_DELAY_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*delay(?:\[[^\]]*\]|\([^)]*\))\s*[^;]*;\s*$"  # QASM3 uses delay[dur] q;
)
_CONSTRAINT_IF_RE: Final[re.Pattern[str]] = re.compile(r"^\s*if\s*\([^)]*\)\s*[^;]*;\s*$")
_DECLARATION_ONLY_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:const\s+)?(?:int|uint|bit|float|angle|bool|string)\s*.*;\s*$"
)

#: Explicit denylist consulted *before* the allowlist. Matches are reported with
#: a precise reason so consumers receive actionable 422 responses.
_BANNED_PATTERNS: Final[tuple[tuple[re.Pattern[str], str, str], ...]] = (
    (
        re.compile(r"\binclude\s*<"),
        "FILE_INCLUDE_BANNED",
        "angle-bracket include directives are forbidden",
    ),
    (re.compile(r"^\s*pragma\b"), "PRAGMA_BANNED", "pragma statements are forbidden"),
    (
        re.compile(r"\b(def|function|extern|gate)\b"),
        "CUSTOM_DEFINITION_BANNED",
        "custom definition blocks are forbidden",
    ),
    (
        re.compile(r"\b(opaque|input|output)\b"),
        "OPAQUE_DECLARATION_BANNED",
        "opaque/input/output declarations are forbidden",
    ),
    (
        re.compile(r"\b(while|repeat|until|for)\b"),
        "UNBOUNDED_LOOP_BANNED",
        "loop constructs are forbidden in submitted circuits",
    ),
    (
        re.compile(r"\b(box|stretch|defcal)\b"),
        "ADVANCED_CONSTRUCT_BANNED",
        "box/stretch/defcal constructs are forbidden",
    ),
    (re.compile(r"\$"), "INTRINSIC_BANNED", "runtime intrinsic calls are forbidden"),
    (re.compile(r"\b(import|from)\b"), "IMPORT_BANNED", "module imports are forbidden"),
)

_ALLOWED_STATEMENT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _VERSION_PRAGMA_RE,
    _INCLUDE_RE,
    _QUBIT_DECL_RE,
    _QREG_DECL_RE,
    _CREG_DECL_RE,
    _MEASURE_RE,
    _BARRIER_RE,
    _RESET_RE,
    _DELAY_RE,
    _CONSTRAINT_IF_RE,
    _DECLARATION_ONLY_RE,
)

#: Gate applications must name a built-in gate from the OpenQASM 3 standard
#: library (``stdgates.inc``). Custom ``gate`` definitions are banned outright,
#: so *every* application is provably a builtin — unknown names are rejected
#: instead of being handed to the parser (defense against parser fuzzing and
#: hostile instruction injection such as ``launch_missiles q[0];``).
_KNOWN_GATES: Final[frozenset[str]] = frozenset(
    {
        # single-qubit rotations & cliffsords
        "id", "i", "x", "y", "z", "h", "s", "sdg", "t", "tdg",
        "sx", "sxdg", "rx", "ry", "rz", "u", "u1", "u2", "u3", "p",
        # two-qubit
        "cx", "cnot", "cy", "cz", "ch", "cp", "crx", "cry", "crz",
        "csx", "csxdg", "swap", "iswap", "rxx", "ryy", "rzz",
        # three-qubit
        "ccx", "ccrx", "ccry", "ccrz", "cswap",
    }
)

#: Registers whose width contributes to the qubit-count firewall.
_QUBIT_DECL_NAMES: Final[frozenset[str]] = frozenset({"qubit", "qreg"})


def _strip_noise(source: str) -> str:
    """Remove comments and string literals so they cannot smuggle statements."""
    without_comments = _COMMENT_RE.sub(" ", source)
    return _STRING_LITERAL_RE.sub('""', without_comments)


def _json_nesting_depth(value: Any, depth: int = 0) -> int:
    """Compute the maximum structural nesting depth of a JSON-like structure."""
    if isinstance(value, dict):
        child = max((_json_nesting_depth(v, depth + 1) for v in value.values()), default=depth + 1)
        return max(depth + 1, child)
    if isinstance(value, list):
        child = max((_json_nesting_depth(v, depth + 1) for v in value), default=depth + 1)
        return max(depth + 1, child)
    return depth + 1


def extract_qubit_count(circuit_data: str | dict[str, Any], fmt: CircuitFormat) -> int:
    """Extract the declared qubit width of a payload for the cost firewall.

    Raises:
        CircuitValidationError: if the payload is structurally unreadable.
        QubitLimitExceededError: if the width exceeds :data:`MAX_QUBITS`.
    """
    observed = _observe_qubit_count(circuit_data, fmt)
    if observed > MAX_QUBITS:
        raise QubitLimitExceededError(observed, MAX_QUBITS)
    return observed


def _observe_qubit_count(circuit_data: str | dict[str, Any], fmt: CircuitFormat) -> int:
    """Count declared qubits without applying the limit (internal helper)."""
    if fmt is CircuitFormat.QASM3:
        if not isinstance(circuit_data, str):
            raise CircuitValidationError(
                "qasm3 payloads must be provided as a string", code="PAYLOAD_TYPE_MISMATCH"
            )
        cleaned = _strip_noise(circuit_data)
        total = 0
        for line in cleaned.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            match = _QUBIT_DECL_RE.match(stripped) or _QREG_DECL_RE.match(stripped)
            if match:
                total += int(match.group(1) or 1)
        if total == 0:
            raise CircuitValidationError(
                "no qubit declarations found in OpenQASM 3.0 payload", code="NO_QUBITS_DECLARED"
            )
        return total

    if not isinstance(circuit_data, dict):
        raise CircuitValidationError(
            f"{fmt.value} payloads must be provided as a JSON object", code="PAYLOAD_TYPE_MISMATCH"
        )

    for key in ("num_qubits", "n_qubits", "qubit_count"):
        raw = circuit_data.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    if isinstance(circuit_data.get("qubits"), int):
        return int(circuit_data["qubits"])
    if isinstance(circuit_data.get("qubits"), list):
        return len(circuit_data["qubits"])

    raise CircuitValidationError(
        f"unable to determine qubit count for {fmt.value} payload; "
        "supply a 'num_qubits' integer field",
        code="NO_QUBITS_DECLARED",
    )


def _validate_qasm3(source: str) -> None:
    """Run the full allowlist grammar over an OpenQASM 3.0 document."""
    if len(source.encode("utf-8")) > MAX_CIRCUIT_BYTES:
        raise CircuitValidationError(
            f"circuit_data exceeds the {MAX_CIRCUIT_BYTES} byte platform ceiling",
            code="PAYLOAD_TOO_LARGE",
        )

    raw_lines = source.splitlines()
    if len(raw_lines) > MAX_CIRCUIT_LINES:
        raise CircuitValidationError(
            f"circuit_data exceeds the {MAX_CIRCUIT_LINES} line ceiling",
            code="PAYLOAD_TOO_LONG",
        )

    cleaned = _strip_noise(source)
    saw_declaration = False
    for number, raw_line in enumerate(cleaned.splitlines(), start=1):
        statement = raw_line.strip()
        if not statement:
            continue

        banned_hit: tuple[str, str] | None = None
        for pattern, code, message in _BANNED_PATTERNS:
            if pattern.search(statement):
                banned_hit = (code, message)
                break
        if banned_hit is not None:
            code, message = banned_hit
            raise CircuitValidationError(message, code=code, line=number)

        if _QUBIT_DECL_RE.match(statement) or _QREG_DECL_RE.match(statement):
            saw_declaration = True
            continue

        if any(pattern.match(statement) for pattern in _ALLOWED_STATEMENT_PATTERNS):
            continue

        gate_match = _GATE_APP_RE.match(statement)
        if gate_match is not None:
            gate_name = gate_match.group(1)
            if gate_name in _KNOWN_GATES:
                continue
            raise CircuitValidationError(
                f"unknown gate {gate_name!r}; only OpenQASM 3 standard-library "
                "gates are permitted (custom gate definitions are banned)",
                code="STATEMENT_NOT_ALLOWED",
                line=number,
            )

        raise CircuitValidationError(
            "statement is not part of the permitted OpenQASM 3.0 allowlist",
            code="STATEMENT_NOT_ALLOWED",
            line=number,
        )

    if not saw_declaration:
        raise CircuitValidationError(
            "no qubit declaration found in OpenQASM 3.0 payload",
            code="NO_QUBITS_DECLARED",
        )


def _validate_json_payload(circuit_data: dict[str, Any], fmt: CircuitFormat) -> None:
    """Structurally validate a serialized Qiskit / Cirq document."""
    if _json_nesting_depth(circuit_data) > MAX_JSON_NESTING:
        raise CircuitValidationError(
            f"JSON payload nesting exceeds {MAX_JSON_NESTING} levels",
            code="PAYLOAD_TOO_DEEP",
        )

    if fmt is CircuitFormat.QISKIT_JSON:
        allowed = {"num_qubits", "n_qubits", "qubit_count", "qubits", "num_clbits", "qasm",
                   "instructions", "operations", "creg", "cregs", "metadata", "global_phase",
                   "circuit", "circuits", "qpy", "version", "clbits"}
        unknown = set(circuit_data) - allowed
        if unknown:
            raise CircuitValidationError(
                f"qiskit_json payload contains disallowed keys: {sorted(unknown)}",
                code="JSON_KEY_NOT_ALLOWED",
            )
        ops = circuit_data.get("instructions") or circuit_data.get("operations")
        if ops is not None:
            if not isinstance(ops, list):
                raise CircuitValidationError(
                    "'instructions' must be a list of operation objects",
                    code="JSON_SCHEMA_VIOLATION",
                )
            if len(ops) > 20_000:
                raise CircuitValidationError(
                    "operation list exceeds 20000 entries",
                    code="PAYLOAD_TOO_LONG",
                )
            for index, op in enumerate(ops):
                if not isinstance(op, dict) or not isinstance(op.get("name"), str):
                    raise CircuitValidationError(
                        f"instruction at index {index} must be an object with a 'name' string",
                        code="JSON_SCHEMA_VIOLATION",
                    )
        return

    # Cirq documents are validated against a permissive but bounded shape; the
    # authoritative gate allowlist is re-applied by the worker-side translator.
    if not any(key in circuit_data for key in ("circuit", "qubits", "num_qubits", "n_qubits")):
        raise CircuitValidationError(
            "cirq_json payload must contain a 'circuit' or 'qubits' field",
            code="JSON_SCHEMA_VIOLATION",
        )


def validate_circuit_payload(circuit_data: str | dict[str, Any], fmt: CircuitFormat) -> int:
    """Run the complete firewall over a payload and return its qubit width.

    This is the single entry point invoked by the FastAPI layer prior to
    persistence and queueing. It never mutates its inputs.

    Args:
            circuit_data: Raw circuit source string or parsed dictionary.
            fmt: Declared serialization format.

    Returns:
            The declared qubit count (always ``<= MAX_QUBITS``).

    Raises:
            CircuitValidationError: on any firewall violation.
            QubitLimitExceededError: when the qubit ceiling is exceeded.
    """
    if fmt is CircuitFormat.QASM3:
        if not isinstance(circuit_data, str):
            raise CircuitValidationError(
                "qasm3 payloads must be provided as a string", code="PAYLOAD_TYPE_MISMATCH"
            )
        _validate_qasm3(circuit_data)
    else:
        if not isinstance(circuit_data, dict):
            raise CircuitValidationError(
                f"{fmt.value} payloads must be provided as a JSON object",
                code="PAYLOAD_TYPE_MISMATCH",
            )
        _validate_json_payload(circuit_data, fmt)

    return extract_qubit_count(circuit_data, fmt)
