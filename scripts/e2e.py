#!/usr/bin/env python3
"""End-to-end smoke test for the Quac local environment.

Walks the entire production path against docker-compose services:

    1. API liveness probe (open route)
    2. Auth surface: unauthenticated /v1/quantum/simulate -> 401
    3. Register user A (201 + session cookie), duplicate -> 409,
       mint an API key (secret shown once)
    4. Register user B + key (the second tenant)
    5. Validation-firewall rejection with valid credentials
       (negative case, 422, zero infra writes)
    6. Bell-state submission with X-API-Key -> 202 + UUIDv4 job_id
    7. Cross-tenant isolation: B reads A's job -> 404; listings are disjoint
    8. Poll GET /v1/quantum/jobs/{id} until a terminal state
    9. Verify the result object exists in S3 at results/{Y}/{M}/{D}/{job_id}.json
       and that its counts look like a real Bell-state distribution
   10. Verify the signed webhook callback arrived at the local sink
   11. Verify monthly usage counters (API-side requests, worker compute_ms)
   12. Revoke A's key -> the same key now fails with 401
   13. Ownership migration: seed a legacy pre-P1 row (no entity/owner),
       run scripts/migrate_ownership.py, verify backfill + idempotency

Exit code 0 = whole path healthy; non-zero = the first fatal leg.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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
TABLE = os.environ.get("QUAC_JOBS_TABLE", "quac-jobs")
MIGRATE_SCRIPT = os.environ.get("QUAC_MIGRATE_SCRIPT", "/scripts/migrate_ownership.py")

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


def _register(client: httpx.Client, username: str) -> httpx.Response:
    """Register a user; the session cookie lands in the client jar."""
    return client.post(
        f"{API_BASE}/v1/auth/register",
        json={"username": username, "password": "e2e-password-123"},
    )


def _mint_key(client: httpx.Client) -> tuple[str, str]:
    """Create an API key with the jar's session; return (key_id, full token)."""
    r = client.post(f"{API_BASE}/v1/keys", json={"label": "e2e"})
    if r.status_code != 201:
        raise RuntimeError(f"key creation failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    return str(body["key_id"]), str(body["key"])


def _migrate(dry_run: bool) -> tuple[int, str]:
    """Run the ownership migration script; return (updated_count, output)."""
    cmd = [sys.executable, MIGRATE_SCRIPT]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError(f"migration script exited {proc.returncode}: {output[-500:]}")
    match = re.search(r"(?:would update|updated) (\d+)", output)
    return (int(match.group(1)) if match else -1), output


def main() -> int:
    print(f"[e2e] api={API_BASE} sink={SINK_BASE} s3={ENDPOINT}", flush=True)

    # ------------------------------------------------------------------ 1. health
    step("1. API liveness")
    client = httpx.Client(timeout=30.0)
    try:
        try:
            r = client.get(f"{API_BASE}/healthz")
        except httpx.HTTPError as exc:
            check("GET /healthz reachable", False, str(exc))
            return 1
        check("GET /healthz -> 200", r.status_code == 200, f"got {r.status_code}")
        if r.status_code != 200:
            return 1

        # ------------------------------------------------- 2. auth is mandatory
        step("2. Unauthenticated quantum routes are refused")
        r = client.post(
            f"{API_BASE}/v1/quantum/simulate",
            json={"circuit_data": BELL_QASM3, "format": "qasm3", "shots": 16},
        )
        ok = r.status_code == 401 and "UNAUTHENTICATED" in json.dumps(r.json())
        check("POST simulate without credentials -> 401", ok, f"got {r.status_code}")
        r = client.get(f"{API_BASE}/v1/quantum/jobs")
        check("GET /jobs without credentials -> 401", r.status_code == 401, f"got {r.status_code}")
        r = client.get(f"{API_BASE}/v1/quantum/formats")
        check("GET /formats stays open -> 200", r.status_code == 200, f"got {r.status_code}")

        # ------------------------------------------------------- 3. register A
        step("3. Register tenant A (session cookie) and mint an API key")
        user_a = f"e2e-a-{uuid.uuid4().hex[:8]}"
        r = _register(client, user_a)
        if not check(
            "POST /auth/register -> 201",
            r.status_code == 201,
            f"got {r.status_code} {r.text[:200]}",
        ):
            return 1
        body = r.json()
        check("registration lands on free plan", body.get("plan") == "free", str(body.get("plan")))
        check(
            "plan limits returned",
            body.get("limits", {}).get("max_qubits") == 12,
            str(body.get("limits", {}).get("max_qubits")),
        )
        dup = _register(client, user_a)
        check(
            "duplicate username -> 409 USERNAME_TAKEN",
            dup.status_code == 409 and "USERNAME_TAKEN" in json.dumps(dup.json()),
            f"got {dup.status_code}",
        )
        key_id_a, key_a = _mint_key(client)
        check("API key minted (secret shown once)", key_a.startswith(f"qk_{key_id_a}_"))
        # The secret must never appear in listings.
        listing = client.get(f"{API_BASE}/v1/keys")
        check(
            "key listing never exposes the secret",
            listing.status_code == 200 and key_a not in json.dumps(listing.json()),
        )

        # ------------------------------------------------------- 4. register B
        step("4. Register tenant B (second tenant)")
        user_b = f"e2e-b-{uuid.uuid4().hex[:8]}"
        r = _register(client, user_b)
        if not check("second registration -> 201", r.status_code == 201, f"got {r.status_code}"):
            return 1
        key_id_b, key_b = _mint_key(client)
        check("tenant B key minted", key_b.startswith(f"qk_{key_id_b}_"))
        # Session jar now belongs to B; quantum calls below authenticate by
        # explicit X-API-Key headers (header takes precedence over cookie).

        # ------------------------------------------------------- 5. firewall (negative)
        step("5. Validation firewall rejects banned circuit pre-queue")
        r = client.post(
            f"{API_BASE}/v1/quantum/simulate",
            json={
                "circuit_data": BANNED_QASM3,
                "format": "qasm3",
                "shots": 16,
                "webhook_url": WEBHOOK_URL,
            },
            headers={"X-API-Key": key_a},
        )
        ok = r.status_code == 422 and "FILE_INCLUDE_BANNED" in json.dumps(r.json())
        check("banned circuit -> 422 FILE_INCLUDE_BANNED", ok, f"status={r.status_code}")

        # ----------------------------------------------------------- 6. submit
        step("6. Submit Bell-state circuit with A's API key")
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
            headers={"X-API-Key": key_a},
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

        # ------------------------------------------------- 7. tenant isolation
        step("7. Tenant isolation (B cannot see A's job)")
        foreign = client.get(
            f"{API_BASE}/v1/quantum/jobs/{job_id}", headers={"X-API-Key": key_b}
        )
        missing = client.get(
            f"{API_BASE}/v1/quantum/jobs/{uuid.uuid4()}", headers={"X-API-Key": key_b}
        )
        ok = (
            foreign.status_code == missing.status_code == 404
            and foreign.json().get("detail", {}).get("error")
            == missing.json().get("detail", {}).get("error")
            == "JOB_NOT_FOUND"
        )
        check("foreign job id -> 404 identical to missing id", ok, f"got {foreign.status_code}")
        owner_view = client.get(
            f"{API_BASE}/v1/quantum/jobs/{job_id}", headers={"X-API-Key": key_a}
        )
        check("owner still sees own job -> 200", owner_view.status_code == 200)

        foreign_cancel = client.post(
            f"{API_BASE}/v1/quantum/jobs/{job_id}/cancel", headers={"X-API-Key": key_b}
        )
        check("foreign cancel -> 404 (no write)", foreign_cancel.status_code == 404)

        list_a = client.get(f"{API_BASE}/v1/quantum/jobs", headers={"X-API-Key": key_a})
        list_b = client.get(f"{API_BASE}/v1/quantum/jobs", headers={"X-API-Key": key_b})
        ids_a = [row.get("job_id") for row in list_a.json().get("jobs", [])]
        ids_b = [row.get("job_id") for row in list_b.json().get("jobs", [])]
        check(
            "A's listing contains the job; B's does not",
            list_a.status_code == 200
            and job_id in ids_a
            and list_b.status_code == 200
            and job_id not in ids_b,
            f"len(A)={len(ids_a)} len(B)={len(ids_b)}",
        )
        check(
            "listings are disjoint",
            not set(ids_a) & set(ids_b),
            f"overlap={sorted(set(ids_a) & set(ids_b))}",
        )

        # ------------------------------------------------------------------- 8. poll
        step("8. Poll job status until terminal")
        deadline = time.monotonic() + POLL_TIMEOUT_S
        final_status = "unknown"
        while time.monotonic() < deadline:
            r = client.get(
                f"{API_BASE}/v1/quantum/jobs/{job_id}", headers={"X-API-Key": key_a}
            )
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

        # ----------------------------------------------------------- 9. S3 result object
        step("9. Verify result object in S3")
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

        # ------------------------------------------------------------------ 10. webhook leg
        step("10. Verify signed webhook callback at sink")
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

        # ------------------------------------------------------------------ 11. usage counters
        step("11. Verify monthly usage counters")
        me = client.get(f"{API_BASE}/v1/auth/me", headers={"X-API-Key": key_a})
        usage = (me.json().get("usage") or {}) if me.status_code == 200 else {}
        check(
            "API recorded >= 1 monthly request",
            me.status_code == 200 and int(usage.get("requests", 0)) >= 1,
            f"status={me.status_code} requests={usage.get('requests')}",
        )
        # The worker ADDs compute time after success; allow a brief settle window.
        compute_ms = int(usage.get("compute_ms", 0))
        settle_deadline = time.monotonic() + 15
        while compute_ms <= 0 and time.monotonic() < settle_deadline:
            time.sleep(1)
            me = client.get(f"{API_BASE}/v1/auth/me", headers={"X-API-Key": key_a})
            usage = (me.json().get("usage") or {}) if me.status_code == 200 else {}
            compute_ms = int(usage.get("compute_ms", 0))
        check("worker recorded compute_ms > 0", compute_ms > 0, f"compute_ms={compute_ms}")
        check(
            "usage is tenant-scoped to A",
            len(usage.get("month", "")) == 7,
            str(usage.get("month")),
        )

        # ------------------------------------------------------------------ 12. revoke key
        step("12. Revoke A's API key")
        revoked = client.delete(f"{API_BASE}/v1/keys/{key_id_a}", headers={"X-API-Key": key_a})
        check("DELETE /keys/{id} -> 200", revoked.status_code == 200, f"got {revoked.status_code}")
        after = client.get(f"{API_BASE}/v1/auth/me", headers={"X-API-Key": key_a})
        check(
            "revoked key now -> 401",
            after.status_code == 401 and "UNAUTHENTICATED" in json.dumps(after.json()),
            f"got {after.status_code}",
        )
        # B's key is unaffected.
        check(
            "tenant B key unaffected",
            client.get(
                f"{API_BASE}/v1/auth/me", headers={"X-API-Key": key_b}
            ).status_code
            == 200,
        )

        # ------------------------------------------------------------- 13. migration leg
        step("13. Ownership migration backfills a legacy pre-P1 row")
        ddb = boto3.resource("dynamodb", endpoint_url=ENDPOINT, region_name=REGION)
        table = ddb.Table(TABLE)
        legacy_id = str(uuid.uuid4())
        table.put_item(
            Item={
                # Deliberately missing entity / owner_id / schema_version —
                # exactly the shape rows written before P1 carry.
                "job_id": legacy_id,
                "status": "completed",
                "format": "qasm3",
                "shots": 16,
                "qubit_count": 1,
                "payload": "{}",
                "created_at": "2025-01-01T00:00:00+00:00",
            }
        )
        would_update, dry_out = _migrate(dry_run=True)
        check(
            "dry-run reports exactly the legacy row",
            would_update == 1,
            dry_out.strip().splitlines()[-1],
        )
        updated, run_out = _migrate(dry_run=False)
        check(
            "migration updates exactly the legacy row",
            updated == 1,
            run_out.strip().splitlines()[-1],
        )
        rerun, rerun_out = _migrate(dry_run=False)
        check(
            "second run is a no-op (idempotent)",
            rerun == 0,
            rerun_out.strip().splitlines()[-1],
        )

        back = table.get_item(Key={"job_id": legacy_id}).get("Item", {})
        check("entity stamped 'job'", back.get("entity") == "job", str(back.get("entity")))
        check(
            "owner defaulted to system",
            back.get("owner_id") == "system",
            str(back.get("owner_id")),
        )
        check(
            "schema_version stamped 1",
            back.get("schema_version") == 1,
            str(back.get("schema_version")),
        )

        # The migrated system-owned row must stay invisible to both tenants.
        list_a = client.get(f"{API_BASE}/v1/quantum/jobs", headers={"X-API-Key": key_a})
        ids_a = [row.get("job_id") for row in list_a.json().get("jobs", [])]
        check(
            "migrated legacy row not listed for tenant A",
            legacy_id not in ids_a,
            f"len(A)={len(ids_a)}",
        )

        # ------------------------------------------------------------------------ summary
        print(flush=True)
        if _failures:
            print(f"[e2e] FAILED ({len(_failures)} check(s)): {_failures}", flush=True)
            return 1
        print(f"[e2e] SUCCESS — full path healthy for job {job_id}", flush=True)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
