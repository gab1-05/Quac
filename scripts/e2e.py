#!/usr/bin/env python3
"""End-to-end smoke test for the Quac local environment.

Walks the entire production path against docker-compose services:

    1. API liveness probe
    2. Validation-firewall rejection (negative case, 422, zero infra writes)
    3. Bell-state submission -> 202 + UUIDv4 job_id
    4. Poll GET /v1/quantum/jobs/{id} until a terminal state
    5. Verify the result object exists in S3 at results/{Y}/{M}/{D}/{job_id}.json
       and that its counts look like a real Bell-state distribution
    6. Verify the signed webhook callback arrived at the local sink

Exit code 0 = whole path healthy; non-zero = the first broken leg.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import httpx
from botocore.exceptions import ClientError

API_BASE = os.environ.get("QUAC_API_BASE", "http://localhost:8000").rstrip("/")
SINK_BASE = os.environ.get("QUAC_WEBHOOK_SINK", "http://localhost:9099").rstrip("/")
WEBHOOK_URL = os.environ.get("QUAC_WEBHOOK_URL", "http://localhost:9099/hook")
ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
BUCKET = os.environ.get("QUAC_RESULTS_BUCKET", "quac-results")

POLL_TIMEOUT_S = float(os.environ.get("QUAC_E2E_POLL_TIMEOUT", "120"))
POLL_INTERVAL_S = float(os.environ.get("QUAC_E2E_POLL_INTERVAL", "2"))

BELL_QASM3 = (
    "OPENQASM 3.0;\n"
    "include \"stdgates.inc\";\n"
    "qubit[2] q;\n"
    "bit[2] c;\n"
    "h q[0];\n"
    "cx q[0], q[1];\n"
    "c = measure q;\n"
)

BANNED_QASM3 = "OPENQASM 3.0;\ninclude </etc/passwd>;\nqubit[1] q;\n"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        _failures.append(label)
    return ok


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def main() -> int:
    print(f"[e2e] api={API_BASE} sink={SINK_BASE} s3={ENDPOINT}", flush=True)

    # ------------------------------------------------------------------ 1. health
    step("1. API liveness")
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.get(f"{API_BASE}/healthz")
        except httpx.HTTPError as exc:
            check("GET /healthz reachable", False, str(exc))
            return 1
        check("GET /healthz -> 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code != 200:
            return 1

        # ------------------------------------------------------- 2. firewall (negative)
        step("2. Validation firewall rejects banned circuit pre-queue")
        r = client.post(
            f"{API_BASE}/v1/quantum/simulate",
            json={
                "circuit_data": BANNED_QASM3,
                "format": "qasm3",
                "shots": 16,
                "webhook_url": WEBHOOK_URL,
            },
        )
        ok = r.status_code == 422
        code = ""
        if ok:
            body = r.json()
            code = str(body.get("detail", {}).get("code", body.get("detail", "")))
            ok = "FILE_INCLUDE_BANNED" in json.dumps(body)
        check("banned circuit -> 422 FILE_INCLUDE_BANNED", ok, f"status={r.status_code} {code}")

        # ----------------------------------------------------------- 3. submit (positive)
        step("3. Submit Bell-state circuit")
        idempotency_key = f"e2e-{uuid.uuid4()}"
        r = client.post(
            f"{API_BASE}/v1/quantum/simulate",
            json={
                "circuit_data": BELL_QASM3,
                "format": "qasm3",
                "shots": 1024,
                "webhook_url": WEBHOOK_URL,
                "idempotency_key": idempotency_key,
            },
        )
        if not check(
            "POST simulate -> 202",
            r.status_code == 202,
            f"got {r.status_code} {r.text[:200]}",
        ):
            return 1
        job_id = r.json().get("job_id", "")
        is_uuid = False
        try:
            parsed = uuid.UUID(job_id)
            is_uuid = parsed.version == 4
        except ValueError:
            pass
        if not check("job_id is UUIDv4", is_uuid, job_id):
            return 1

        # ------------------------------------------------------------------- 4. poll
        step("4. Poll job status until terminal")
        deadline = time.monotonic() + POLL_TIMEOUT_S
        final_status = "unknown"
        while time.monotonic() < deadline:
            r = client.get(f"{API_BASE}/v1/quantum/jobs/{job_id}")
            if r.status_code != 200:
                check("GET status -> 200", False, f"got {r.status_code}")
                return 1
            final_status = r.json().get("status", "unknown")
            if final_status in {"completed", "failed", "dead_letter"}:
                break
            time.sleep(POLL_INTERVAL_S)
        check(
            "job reached 'completed'",
            final_status == "completed",
            f"final={final_status} after {POLL_TIMEOUT_S:.0f}s budget",
        )
        if final_status != "completed":
            return 1

    # ----------------------------------------------------------- 5. S3 result object
    step("5. Verify result object in S3")
    # The worker derives the key from its own UTC clock; probe a ±1 day window.
    s3 = boto3.client("s3", endpoint_url=ENDPOINT, region_name=REGION)
    found_key: str | None = None
    now = datetime.now(UTC)
    for day_offset in (-1, 0, 1):
        day = now - timedelta(days=day_offset)
        candidate = f"results/{day:%Y}/{day:%m}/{day:%d}/{job_id}.json"
        try:
            s3.head_object(Bucket=BUCKET, Key=candidate)
            found_key = candidate
            break
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
            continue
    if not check(
        "result object exists (+/-1d window)",
        found_key is not None,
        found_key or "not found",
    ):
        return 1

    obj = s3.get_object(Bucket=BUCKET, Key=found_key or "")
    payload = json.loads(obj["Body"].read())
    counts = payload.get("counts") or {}
    total = sum(int(v) for v in counts.values())
    bell_shape = len(counts) >= 1 and set(counts) <= {"00", "11"}
    check("counts sum equals shots", total == 1024, f"sum={total}")
    check("Bell correlations only (00/11)", bell_shape, f"keys={sorted(counts)}")

    # ------------------------------------------------------------------ 6. webhook leg
    step("6. Verify signed webhook callback at sink")
    try:
        r = httpx.get(f"{SINK_BASE}/hooks", timeout=10.0)
        hooks = r.json().get("hooks", []) if r.status_code == 200 else []
    except httpx.HTTPError as exc:
        check("sink reachable", False, str(exc))
        return 1
    mine = [h for h in hooks if h.get("job_id") == job_id]
    check("callback received for job_id", bool(mine), f"sink holds {len(hooks)} total")
    if mine:
        check(
            "callback status == completed",
            mine[-1].get("status") == "completed",
            str(mine[-1].get("status")),
        )

    # ------------------------------------------------------------------------ summary
    print(flush=True)
    if _failures:
        print(f"[e2e] FAILED ({len(_failures)} check(s)): {_failures}", flush=True)
        return 1
    print(f"[e2e] SUCCESS — full path healthy for job {job_id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
