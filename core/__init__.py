"""Quac shared core — business logic used by both the API and the worker.

``core`` holds the platform's single source of truth for circuit handling:

* :mod:`core.circuit` — the format factory (``qasm3`` / ``qiskit_json`` /
  ``cirq_json`` → :class:`~qiskit.QuantumCircuit`), imported by the API's
  pre-flight analyzer and by the worker's execution path so both planes parse
  a payload exactly the same way.
* :mod:`core.analysis` — pre-flight analysis: exact structural metrics,
  labelled estimates, structural interaction proxies, warnings and the
  recommendation inputs later consumed by the backend selector.

Both container images copy this package (``Dockerfile.api`` → ``/app/core``,
``Dockerfile.worker`` → ``/var/task/core``); neither plane may import the
other's package.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
