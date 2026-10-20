"""Quac worker package.

Distributed compute worker engine of the Quac Serverless Quantum Simulation
Model-as-a-Service platform. This package contains the AWS Lambda / SQS
consumer handler and the qiskit-aer simulation factory used to execute
validated quantum circuits asynchronously.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
