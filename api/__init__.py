"""Quac API package.

Ingestion layer of the Quac Serverless Quantum Simulation
Model-as-a-Service (MaaS) platform. This package exposes the FastAPI
application, Pydantic contracts, the DynamoDB persistence adapter and the
SQS FIFO publishing service used to accept asynchronous simulation jobs.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
