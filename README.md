# Quac — Serverless Quantum Simulation Model-as-a-Service (MaaS)

**Quac** is a production-grade, serverless platform that accepts quantum circuits
(OpenQASM 3.0 strings or serialized Qiskit/Cirq JSON), processes them
asynchronously on highly optimized classical compute, and streams signed results
back to consumers through a resilient webhook framework.

```
                         ┌──────────────────────────────────────────────────────────┐
                         │                      QUAC PLATFORM                       │
  Client                 │                                                          │
  ──────►  HTTP API Gateway ──► ALB ──► FastAPI on ECS Fargate  (Quac API)          │
              (VPC link)                 │                                          │
                                         │  1. validation firewall (≤28 qubits)     │
                                         │  2. DynamoDB job record                   │
                                         │  3. SQS FIFO enqueue  ──► 202 Accepted    │
                                         ▼                                           │
                                  DynamoDB (job_id HASH)                             │
                                         │                                           │
                                         ▼                                           │
                              SQS FIFO  (+ DLQ, maxReceiveCount=3)                  │
                                         │                                           │
                                         ▼                                           │
                     Lambda worker (container image, qiskit-aer)                    │
                       │  SIGALRM guard @ 9:30 · daemon compute thread              │
                       ├─► S3  results/{Y}/{M}/{D}/{job_id}.json  (SSE-AES256)       │
                       └─► Webhook POST (HMAC-signed, 3× backoff + jitter)           │
                         └──────────────────────────────────────────────────────────┘
```

---

## Repository layout

```text
quantum-maas-platform/
├── api/                        # Ingestion layer (FastAPI on ECS Fargate)
│   ├── __init__.py
│   ├── main.py                 # endpoints, global sliding-window rate limiter
│   ├── schemas.py              # Pydantic v2 contracts + validation firewall
│   ├── database.py             # pooled DynamoDB job store (async wrappers)
│   ├── queue_service.py        # SQS FIFO publisher + dedup-id derivation
│   ├── requirements.txt
│   └── Dockerfile.api          # multi-stage, non-root, gunicorn+uvicorn
├── worker/                     # Compute engine (AWS Lambda container image)
│   ├── __init__.py
│   ├── worker.py               # SQS consumer, timers, S3, webhook dispatcher
│   ├── simulator.py            # qiskit-aer factory + format converters
│   ├── requirements.txt
│   └── Dockerfile.worker       # AWS Lambda Python 3.11 base, ARM64/X86_64 wheels
├── terraform/                  # AWS provider ~> 5.0 (validated with terraform validate)
│   ├── main.tf                 # VPC, ECR, ECS/Fargate, ALB, HTTP API GW, Lambda
│   ├── variables.tf  outputs.tf
│   ├── iam.tf                  # PoLP roles scoped to this workspace's ARNs
│   ├── dynamodb.tf  sqs.tf  s3.tf
├── tests/                      # pytest suite (94 tests)
├── scripts/                    # local end-to-end runtime (LocalStack)
│   ├── localstack_init.py      # idempotent table/queue/bucket bootstrap
│   ├── local_worker.py         # long-poll loop invoking the real lambda_handler
│   ├── webhook_receiver.py     # HMAC-verifying callback sink
│   └── e2e.py                  # drives API → SQS → worker → S3 → webhook
├── docker-compose.yml          # localstack + api + worker + webhook + e2e
├── .github/workflows/ci.yml    # ruff · mypy · pytest · gitleaks · terraform · e2e
├── pyproject.toml              # ruff + mypy + pytest configuration
├── .gitleaks.toml              # secret-scan allowlist (documented example UUID)
└── README.md
```

---

## Quick start

### Run the test suite

```bash
pip install pytest fastapi httpx boto3 qiskit qiskit-aer qiskit-qasm3-import
python -m pytest tests -q
```

### Run the API locally

```bash
pip install -r api/requirements.txt
uvicorn api.main:app --reload --port 8000
```

### Submit a circuit

```bash
curl -sS -X POST http://localhost:8000/v1/quantum/simulate \
  -H 'Content-Type: application/json' \
  -d '{
    "circuit_data": "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[2] q;\nbit[2] c;\nh q[0];\ncx q[0], q[1];\nc = measure q;\n",
    "format": "qasm3",
    "shots": 1024,
    "webhook_url": "https://consumer.example.com/hooks/quac",
    "idempotency_key": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
  }'
```

`202 Accepted`:

```json
{
  "job_id": "3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c",
  "status": "queued",
  "status_url": "/v1/quantum/jobs/3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c",
  "detail": "simulation accepted for asynchronous execution"
}
```

### Track a job

```bash
curl -sS http://localhost:8000/v1/quantum/jobs/3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c
```

```json
{
  "job_id": "3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c",
  "status": "completed",
  "created_at": "2026-10-19T12:00:00+00:00",
  "completed_at": "2026-10-19T12:00:07+00:00",
  "error_message": null
}
```

Status lifecycle: `queued → processing → completed | failed`.

> The tracking endpoint is protected by a **global sliding-window rate limiter**
> (default 120 req/60 s per client IP, `429` + `Retry-After` on excess;
> `QUAC_RATE_LIMIT_REQUESTS` / `QUAC_RATE_LIMIT_WINDOW`).

### Supported formats

| `format`      | `circuit_data` shape                                   | Converter                                            |
|---------------|--------------------------------------------------------|------------------------------------------------------|
| `qasm3`       | OpenQASM source string                                 | `qiskit.qasm3.loads` (v3) / `from_qasm_str` (v2)      |
| `qiskit_json` | `{"qasm": ...}` or `{"num_qubits": N, "instructions": [...]}` | QASM passthrough / generic instruction builder |
| `cirq_json`   | Cirq JSON document                                     | strict gate-allowlist translator → Qiskit            |

---

## Webhook contract

On terminal state Quac POSTs to your `webhook_url`:

```json
{
  "event": "simulation.completed",
  "job_id": "…",
  "status": "completed",
  "shots": 1024,
  "method": "matrix_product_state",
  "num_qubits": 2,
  "depth": 3,
  "most_frequent": "00",
  "top_counts": {"00": 512, "11": 512},
  "simulation_time_ms": 842,
  "completed_at": "2026-10-19T12:00:07+00:00",
  "download_url": "https://…amazons3.com/…?X-Amz-Signature=…",
  "download_expires_in_seconds": 900
}
```

Failures deliver `status: "failed"` with `error` / `error_type` and a null
`download_url` (including dedicated `simulation.timeout` events).

* **Signing** — `X-Quac-Signature: sha256=<HMAC>` over the exact body bytes
  with `QUAC_WEBHOOK_SECRET`; verify before trusting content.
* **Retries** — exactly 3 attempts, exponential backoff (0.5 s base, 8 s cap)
  with random jitter; 4xx responses fail fast (not retried), 5xx/timeouts retry.
* **Delivery is best-effort** — a dead consumer never re-fails the job.

---

## Infrastructure deployment

```bash
cd terraform
terraform init
terraform plan -var="worker_image_uri=<account>.dkr.ecr.<region>.amazonaws.com/quac/prod/worker:latest" \
               -var="api_certificate_arn=arn:aws:acm:…:certificate/…"
terraform apply
```

Build & push images:

```bash
# API (Fargate)
docker build -f api/Dockerfile.api -t $ECR_API:latest .
docker push $ECR_API:latest

# Worker (Lambda) — architecture must match var.worker_architecture
docker buildx build --platform linux/arm64 --build-arg TARGETARCH=arm64 \
  -f worker/Dockerfile.worker -t $ECR_WORKER:latest --push .
```

**Design notes**

* **DynamoDB** — `PAY_PER_REQUEST` (bursty, unpredictable MaaS traffic; rationale
  + a fully written-out provisioned/autoscaling alternative in `dynamodb.tf`),
  Point-in-Time Recovery enabled, TTL on `expires_at`, SSE on.
* **SQS** — FIFO with content-based deduplication *and* API-computed
  `MessageDeduplicationId`; DLQ with `maxReceiveCount = 3`; visibility timeout
  660 s > Lambda's 600 s cap; queue depth alarm on the DLQ.
* **S3** — SSE-AES256 default encryption, all four public-access blocks,
  TLS-only bucket policy, `BucketOwnerEnforced` (ACLs off), lifecycle
  `results/` → **GLACIER_DEEP_ARCHIVE after 30 days**.
* **IAM (PoLP)** — API task role can only write *this* table / send to *this*
  queue / read `results/*`; the Lambda role can only consume *this* queue,
  write `results/*`, and mutate *this* table. No wildcards (except the
  X-Ray + `GetAuthorizationToken` actions that do not support resource scoping).

---

## The enterprise DevOps challenges — how Quac resolves them

### 1 · Serverless cold-start penalty

Heavy initialization (`numpy`, `qiskit`, `qiskit_aer`) runs **at module import
scope** in `worker/simulator.py` and `worker/worker.py`. The Lambda container
runtime imports the handler module during the *initialization* phase — outside
the active handler loop — so C++ extension linkage is paid once per execution
environment, never inside a timed request. Supporting measures:

* pooled `boto3` clients and a module-level `ThreadPoolExecutor` (created once,
  reused on every warm invocation);
* the Lambda handler itself performs zero heavy imports;
* `worker_architecture = arm64` (Graviton price/performance) with wheels pinned
  to `manylinux2014_aarch64`, plus 2048 MB memory to keep scheduling latency low.

### 2 · Circuit validation firewall

All static validation happens in `api/schemas.py` **before the first write**,
so rejected payloads have zero infrastructure footprint (verified by tests):

* 64 KiB / 5 000-line ceilings; JSON nesting capped at 12; instruction lists
  capped at 20 000 entries;
* comments and string literals are stripped before analysis (no comment-borne
  smuggling);
* **denylist first**: angle-bracket `include </etc/passwd>`, `pragma`,
  `def`/`function`/`gate` custom definitions, `opaque`/`input`/`output`,
  unbounded loops (`while`/`repeat`/`for`), `box`/`stretch`/`defcal`, `$…`
  intrinsics, module imports — each with a precise error code and line number;
* **allowlist after**: every statement must match a sanctioned grammar, and
  gate applications must name a built-in `stdgates.inc` gate (so
  `launch_missiles q[0];` is rejected at ingest, not by the parser);
* **28-qubit cost firewall**: `qubit[29] …` → `422 QUBIT_LIMIT_EXCEEDED`
  instantly (2²⁸ complex128 amplitudes ≈ 4 GiB; the wall prevents the bill).

### 3 · Graceful timeout deflections

The worker Lambda is configured at the service maximum (`timeout = 600 s`);
Quac never intends to use the last 30 seconds:

* `signal.setitimer(signal.ITIMER_REAL, 570)` arms a **SIGALRM at 9:30** for
  every record; the handler raises `SimulationTimeoutError` *in the main thread*
  while it waits on the daemon compute thread;
* a secondary budget derived from `context.get_remaining_time_in_millis()`
  backs up the alarm (also used as the `future.result(timeout=…)` bound);
* on timeout the worker writes **`Timeout Error`** + elapsed ms into DynamoDB,
  marks the job `failed`, fires the failure webhook, disarms the timer, and
  **completes the SQS record** — the orphaned compute thread is a daemon,
  reaped when the context freezes, so the message never loops in retries;
* infra failures alone surface as `batchItemFailures` (partial batch
  reporting), so only real AWS faults are redelivered.

---

## Local end-to-end environment

The full production path runs locally against [LocalStack](https://localstack.cloud)
— no AWS account required. `docker-compose.yml` wires five services: LocalStack
(SQS FIFO + DynamoDB + S3), the API image, the worker image (a long-poll loop that
feeds real SQS records into the **identical** `worker.lambda_handler`), a
webhook sink, and an on-demand e2e driver.

```bash
docker compose up -d --build --wait localstack init api worker webhook
docker compose run --rm e2e      # submit → poll → verify S3 + signed callback
docker compose logs -f webhook   # watch HMAC-signed callbacks arrive
```

The `e2e` driver walks all six legs: API liveness, a firewall rejection (422,
zero infra writes), a Bell-state submission (202 + UUIDv4), status polling to a
terminal state, the `results/{Y}/{M}/{D}/{job_id}.json` S3 object (counts sum to
shots, Bell correlations only), and the signed webhook callback at the sink.
Exit code 0 means the entire path is healthy. `scripts/localstack_init.py` is
idempotent and mirrors the Terraform-provisioned resources exactly.

---

## Continuous integration

`.github/workflows/ci.yml` runs three jobs on every push/PR:

| Job         | Steps                                                                 |
|-------------|-----------------------------------------------------------------------|
| `quality`   | ruff · mypy · pytest · gitleaks (against the pinned prod dependencies)|
| `terraform` | `fmt -check` · `init -backend=false` · `validate`                     |
| `e2e`       | builds both images, boots the LocalStack stack, drives the full path  |

The `e2e` job doubles as an image-build gate: it exercises `Dockerfile.api` and
`Dockerfile.worker` end to end, so a broken dependency pin or cross-arch wheel
resolution fails the build, not production.

---

## Security tooling

The repo is scanned by the same chain CI and auditors use. The current baseline:

* **gitleaks** — `0 leaks`. One documented false positive (the example
  `idempotency_key` UUID in `api/schemas.py` / `README.md`) is allow-listed in
  `.gitleaks.toml`.
* **semgrep** (`p/python`) — `0 findings` across all source files.
* **pip-audit** — `0 known vulnerabilities`. One real advisory was remediated
  during the audit: `starlette 0.41.3` carried 14 DoS CVEs that were **not
  patchable within the fastapi 0.115 line** (it caps `starlette<0.47`, while the
  fixes start at `1.0.1`), so `api/requirements.txt` was promoted to
  fastapi 0.149.0 + starlette 1.9.0 + pydantic 2.13.5 — the exact stack the test
  suite runs green against.
* **trivy** — filesystem scan for dependency CVEs, leaked secrets, and IaC /
  Dockerfile / compose misconfigurations.

---

## Testing

```bash
python -m pytest tests -q
```

| File                            | Covers                                                            |
|---------------------------------|-------------------------------------------------------------------|
| `test_schemas_firewall.py`      | allow/deny matrix, injection vectors, 28-qubit cap, Pydantic contract |
| `test_queue_dedup.py`           | dedup-id determinism/precedence, envelope round-trip              |
| `test_api_endpoints.py`         | 202 flow, 404s, rate-limit 429 + `Retry-After`, no-side-effect rejections |
| `test_simulator_factory.py`     | format converters, MPS-vs-statevector selection, Aer execution    |
| `test_worker_webhook.py`        | 3-attempt backoff, HMAC signing, SIGALRM budget, batch reporting  |

Qiskit-dependent modules skip cleanly (`pytest.importorskip`) when
`qiskit-aer` is absent; SIGALRM tests skip on non-POSIX hosts (Lambda is Linux).

## Security posture at a glance

* Non-root container (`uid 10001`), no compilers/packages in runtime images.
* Webhook URLs: strict http/https, credentials-in-URL rejected.
* HMAC-SHA256 signed callbacks; TLS-only bucket/queue policies; SSE on S3,
  SQS and DynamoDB; ECR image scanning + immutable tags; ALB TLS 1.3 policy.
* Least-privilege IAM scoped to workspace ARNs; DLQ + CloudWatch alarms for
  error-rate and poison messages.
