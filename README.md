# Quac — Serverless Quantum Simulation Model-as-a-Service (MaaS)

[![CI](https://github.com/gab1-05/QuacSM/actions/workflows/ci.yml/badge.svg)](https://github.com/gab1-05/QuacSM/actions/workflows/ci.yml)

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
                       ├─► S3  results/{Y}/{M}/{D}/{job_id}.json  (SSE-KMS CMK)      │
                       └─► Webhook POST (HMAC-signed, 3× backoff + jitter)           │
                         └──────────────────────────────────────────────────────────┘
```

---

## Repository layout

```text
quantum-maas-platform/
├── api/                        # Ingestion layer (FastAPI on ECS Fargate)
│   ├── __init__.py
│   ├── main.py                 # quantum endpoints (simulate/analyze/track/result/cancel/list)
│   ├── schemas.py              # Pydantic v2 contracts + validation firewall + auth/jobs models
│   ├── database.py             # pooled DynamoDB job store (create, owner-list, active-count)
│   ├── queue_service.py        # SQS FIFO publisher + dedup-id derivation
│   ├── auth.py                 # argon2id hashing, key/session minting, require_identity dep
│   ├── auth_routes.py          # /v1/auth (register·login·logout·me) + /v1/keys CRUD
│   ├── identity.py             # single-table identity store (users/keys/sessions/usage/audit)
│   ├── plans.py                # plan tiers — every quota ceiling enforced before side effects
│   ├── ratelimit.py            # global per-IP sliding-window limiter (429 + Retry-After)
│   ├── requirements.txt
│   └── Dockerfile.api          # multi-stage, non-root, gunicorn+uvicorn
├── core/                       # Shared package used by both planes
│   ├── circuit.py              # format factory: qasm3 / qiskit_json / cirq_json → QuantumCircuit
│   ├── backends/               # P3 backend layer (pure metadata/logic in base+selector)
│   │   ├── base.py             # capability matrix, Clifford classifier, GPU probe
│   │   ├── selector.py         # deterministic backend selection + typed refusals
│   │   └── classical.py        # Aer implementations (worker-only import)
│   └── analysis.py             # pre-flight analyzer: exact metrics, basis-labelled estimates
├── worker/                     # Compute engine (AWS Lambda container image)
│   ├── __init__.py
│   ├── worker.py               # SQS consumer, timers, S3, webhook dispatcher
│   ├── simulator.py            # qiskit-aer execution via the P3 backend selector/registry
│   ├── requirements.txt
│   └── Dockerfile.worker       # AWS Lambda Python 3.11 base, ARM64/X86_64 wheels
├── terraform/                  # AWS provider ~> 5.0 (validated with terraform validate)
│   ├── main.tf                 # VPC, ECR, ECS/Fargate, ALB, HTTP API GW, Lambda
│   ├── variables.tf  outputs.tf
│   ├── iam.tf                  # PoLP roles scoped to this workspace's ARNs
│   ├── dynamodb.tf  sqs.tf  s3.tf
├── web/                        # React + Vite + TypeScript + Tailwind console
│   ├── src/
│   │   ├── api/
│   │   │   ├── schema.ts       # types generated from the FastAPI OpenAPI spec
│   │   │   └── client.ts       # typed fetch client + normalised ApiError envelope
│   │   ├── components/
│   │   │   ├── Shell.tsx       # nav, identity menu, API status pill, theme toggle
│   │   │   └── ui.tsx          # Card/Badge/Stat/ErrorBanner/Skeleton/…
│   │   ├── lib/
│   │   │   ├── auth.tsx        # AuthProvider: /v1/auth/me boot + login/register/logout
│   │   │   ├── theme.tsx       # dark-first theme provider (dark/light/system)
│   │   │   ├── format.ts       # bytes/seconds/USD/timestamp display helpers
│   │   │   └── hooks.ts        # useDebouncedValue
│   │   ├── pages/
│   │   │   ├── AuthPage.tsx    # /login + /register (session-cookie auth)
│   │   │   ├── Overview.tsx    # API status, platform limits, account usage, recent jobs
│   │   │   ├── Studio.tsx      # circuit editor + live analyze + run
│   │   │   ├── Jobs.tsx        # server-side job list: cursor pages, status filter, id lookup
│   │   │   └── JobDetail.tsx   # live status polling + result document + cancel
│   │   └── index.css           # Tailwind v4 + design tokens (dark-first)
│   ├── vite.config.ts          # dev proxy /v1 + /healthz → localhost:8000
│   └── package.json            # react 19, react-router 7, @tanstack/react-query 5
├── tests/                      # pytest suite (294 tests, 15 files)
├── scripts/                    # local end-to-end runtime (LocalStack)
│   ├── localstack_init.py      # idempotent table/queue/bucket bootstrap
│   ├── local_worker.py         # long-poll loop invoking the real lambda_handler
│   ├── webhook_receiver.py     # HMAC-verifying callback sink
│   ├── migrate_ownership.py    # backfill pre-P1 rows (entity/owner/schema), idempotent
│   └── e2e.py                  # 13-leg driver: auth → submit → worker → S3 → webhook → usage
├── docker-compose.yml          # localstack + api + worker + webhook + e2e
├── .github/workflows/ci.yml    # ruff · mypy · pytest · gitleaks · terraform · web · e2e
├── pyproject.toml              # ruff + mypy + pytest configuration
├── .gitleaks.toml              # secret-scan allowlist (documented example UUID)
└── README.md
```

---

## Quick start

### Run the test suite

```bash
pip install pytest fastapi httpx boto3 qiskit qiskit-aer qiskit-qasm3-import cirq-core
python -m pytest tests -q
```

### Run the API locally

```bash
pip install -r api/requirements.txt
uvicorn api.main:app --reload --port 8000
```

### Authenticate — sessions and API keys (P1)

Every `/v1/*` route requires an identity **except** the four open routes:

| Open route                  | Why it is public                                  |
|-----------------------------|---------------------------------------------------|
| `GET /healthz`              | liveness probe (no tenant data)                   |
| `GET /v1/quantum/formats`   | capability discovery (platform limits, no data)   |
| `POST /v1/auth/register`    | open sign-up (lands on the free plan)             |
| `POST /v1/auth/login`       | credential exchange                               |

Anything else without credentials returns the single generic envelope
`401 {"detail": {"error": "UNAUTHENTICATED", "message": …}}` — no username
oracle, no route disclosure (unknown logins verify against a dummy argon2 hash).

**Register or log in** (the API sets an HTTP-only `quac_session` cookie):

```bash
curl -sS -c quac.cookies -X POST http://localhost:8000/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username": "acme-labs", "password": "correct horse battery staple"}'
```

* username: 3-32 chars `[a-z0-9_-]`, must start alphanumeric (case is folded to
  lowercase, so `Alice` and `alice` are the same account); duplicate →
  `409 USERNAME_TAKEN`.
* password: 10-128 chars, stored as **argon2id** peppered with
  `QUAC_SECRET_KEY` (compose sets `local-dev-secret`; terraform wires a
  sensitive var — never committed).
* `POST /v1/auth/login` takes the same body; `GET /v1/auth/me` returns your
  identity, plan ceilings and this month's usage counters; `POST /v1/auth/logout`
  deletes the session and clears the cookie (idempotent).

**Or mint an API key** (recommended for automation — created from the session):

```bash
curl -sS -b quac.cookies -X POST http://localhost:8000/v1/keys \
  -H 'Content-Type: application/json' -d '{"label": "ci-runner"}'
```

```json
{
  "key_id": "6f0d9c2a1b4e8d70",
  "key": "qk_6f0d9c2a1b4e8d70_<32-char secret>",
  "prefix": "qk_6f0d9c2a1b4e8d70",
  "label": "ci-runner",
  "created_at": "2026-10-21T04:00:00+00:00"
}
```

* `key` is shown **exactly once** — only a peppered HMAC-SHA256 hash is
  stored, and `GET /v1/keys` (list metadata) never returns secrets.
* Send it as `-H 'X-API-Key: qk_…'`. The header **takes precedence over the
  cookie** when both are present.
* `DELETE /v1/keys/{key_id}` revokes instantly; the next request with that key
  gets `401` (revoked cross-tenant → `404`, no existence oracle).

Every example below includes the credential header — replace `$QUAC_KEY` with
your `key` (or swap in `-b quac.cookies` for cookie auth).

### Submit a circuit

```bash
curl -sS -X POST http://localhost:8000/v1/quantum/simulate \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $QUAC_KEY" \
  -d '{
    "circuit_data": "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[2] q;\nbit[2] c;\nh q[0];\ncx q[0], q[1];\nc = measure q;\n",
    "format": "qasm3",
    "shots": 1024,
    "webhook_url": "https://consumer.example.com/hooks/quac",
    "idempotency_key": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
  }'
```

`webhook_url` is **optional** — omit it entirely (or send `null`) for
polling-only consumers such as the dashboard; results are then retrieved from
the result endpoint below. When present it must be a credential-free
http/https URL (credentials-in-URL → `422`).

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
curl -sS http://localhost:8000/v1/quantum/jobs/3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c \
  -H "X-API-Key: $QUAC_KEY"
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

Status lifecycle: `queued → processing → completed | failed | cancelled`.

> The tracking endpoint is protected by a **global sliding-window rate limiter**
> (default 120 req/60 s per client IP, `429` + `Retry-After` on excess;
> `QUAC_RATE_LIMIT_REQUESTS` / `QUAC_RATE_LIMIT_WINDOW`).

### Fetch a result

```bash
curl -sS http://localhost:8000/v1/quantum/jobs/3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c/result \
  -H "X-API-Key: $QUAC_KEY"
```

* `409 JOB_NOT_READY` while the job is still `queued`/`processing`;
* `200` with `result` (shots, counts, method, backend + selection reasons,
  depth, timings, reproducibility
  metadata) and a short-lived **pre-signed S3 `download_url`** for the full
  result document (TTL `QUAC_PRESIGNED_TTL`, default 900 s) once `completed`;
* `200` with `result: null` + `error_message` for `failed`/`cancelled`;
* `503 RESULT_UNAVAILABLE` if the artifact store cannot be read.

The API reads the worker-written `results/{Y}/{M}/{D}/{job_id}.json` object
from `QUAC_RESULTS_BUCKET` (default `quac-results`); the task role is scoped to
`s3:GetObject` on `results/*` only.

### Cancel a job

```bash
curl -sS -X POST http://localhost:8000/v1/quantum/jobs/3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c/cancel \
  -H "X-API-Key: $QUAC_KEY"
```

Cancellation is atomic: a conditional DynamoDB transition moves a
`queued`/`processing` job to `cancelled` and can never overwrite a terminal
state (`404` unknown id, `409 JOB_NOT_CANCELLABLE` if it already finished).
The worker re-checks before executing, discards a result that finalises after
the cancel (deleting the orphaned artifact) and delivers a
`simulation.cancelled` callback when a webhook is configured.

### Analyze a circuit (pre-flight)

```bash
curl -sS -X POST http://localhost:8000/v1/quantum/analyze \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $QUAC_KEY" \
  -d '{"circuit_data": "OPENQASM 3.0;\ninclude \"stdgates.inc\";\nqubit[2] q;\nbit[2] c;\nh q[0];\ncx q[0], q[1];\nc = measure q;\n", "format": "qasm3", "shots": 1024}'
```

Returns the full pre-flight report **synchronously, with zero side effects** —
no job record, no queue message, no object-storage write. The exact same
validation firewall runs first, so every rejection code (`422 QUBIT_LIMIT_EXCEEDED`,
`422 JSON_SCHEMA_VIOLATION`, `422 NO_QUBITS_DECLARED`, …) matches simulation
ingestion.

Verified values for the Bell circuit above: `shots: 1024`, `depth: 3`,
`gate_histogram: {"h": 1, "cx": 1, "measure": 2}`,
`estimates.statevector_memory_bytes: {value: 64, basis: "exact"}`,
`recommendation.backend: "stabilizer"`, `warnings: []` — Bell is entirely
Clifford, so the selector routes it to the exact polynomial-cost stabilizer
backend (see [Backend selection](#backend-selection-p3)).

**Methodology** — every number carries a `basis` label so nothing is
misrepresented:

* `exact` — computed directly from the parsed circuit (depth, histogram,
  interaction/critical-path proxies, `16 B × 2^qubits` statevector memory);
* `estimated` — documented heuristics with the formula in the `note` field
  (runtime band, cost uses the verified AWS Lambda us-east-1 x86 price of
  `$0.0000166667/GB-s` + `$0.20/1M requests` at the worker's 2 GB memory);
* `rule` — deterministic backend selection shared verbatim with the worker
  (Clifford → stabilizer, width/memory guards → MPS, depth rule → dense
  statevector; see below).

Entanglement is **not** computed; the `interaction` block reports structural
graph proxies only.

### Supported formats

| `format`      | `circuit_data` shape                                   | Converter                                            |
|---------------|--------------------------------------------------------|------------------------------------------------------|
| `qasm3`       | OpenQASM source string                                 | `qiskit.qasm3.loads` (v3) / `from_qasm_str` (v2)      |
| `qiskit_json` | `{"qasm": ...}` or `{"num_qubits": N, "instructions": [...]}` | QASM passthrough / generic instruction builder |
| `cirq_json`   | bare `cirq.to_json()` document (`{"cirq_type": "Circuit", "moments": [...]}`) **or** envelope `{"num_qubits": N, "circuit": <document-or-json-string>}` | strict gate-allowlist translator → Qiskit            |

### Backend selection (P3)

Every circuit is routed by a deterministic selector
(`core/backends/selector.py`) that the analyzer and the worker run verbatim —
the `recommendation` in the pre-flight report is the backend that will
execute. Selection reasons, the considered set and any caveats are returned
with the report **and** persisted on the result document
(`result.backend`, `result.selection_reasons`).

| Backend         | Aer method             | Device | Max qubits | Clifford-only | When it wins                                            |
|-----------------|------------------------|--------|------------|---------------|---------------------------------------------------------|
| `stabilizer`    | `stabilizer`           | cpu    | 28         | yes           | every operation is Clifford (incl. `rx/ry/rz` at k·π/2) |
| `mps`           | `matrix_product_state` | cpu    | 28         | no            | width > 16, or depth ≤ 40, or statevector memory guard  |
| `statevector`   | `statevector`          | cpu    | 16         | no            | deep (depth > 40) non-Clifford circuits ≤ 16 qubits     |
| `gpu_statevector`| `statevector`         | gpu    | 16         | no            | the statevector case when a GPU is detected             |

Rules, in order:

1. **Clifford → stabilizer.** Exact simulation with cost polynomial in circuit
   size (`qubits² × depth`, never `2^qubits`). Excluded when a noise model is
   present (Aer's stabilizer method does not support general noise); a deep
   Clifford circuit therefore still avoids a dense statevector.
2. **Width guard.** Beyond 16 qubits only MPS survives (dense statevector
   needs `16 B × 2^qubits`); deep-and-wide circuits carry a bond-dimension
   caveat.
3. **Depth rule.** Depth ≤ 40 → MPS (tensor-network memory grows with
   entanglement, not width); deeper non-Clifford circuits → dense statevector,
   falling back to MPS when the vector would exceed half the worker memory
   budget (`QUAC_MEMORY_BUDGET_MB`, default 2048 MiB).
4. **GPU is opt-in.** `gpu_statevector` is only chosen when
   `QUAC_GPU_ENABLED=1` **and** `nvidia-smi` lists a device — never on an
   unverified host.

Manual overrides (`result.backend` via the simulator API) are validated against
the same capability facts and refuse with a typed, human-readable reason
(`BackendUnavailableError`); they are never silently coerced to another backend.

The runtime band's stabilizer branch was calibrated against measured
end-to-end walls inside the pinned production worker image (qiskit 1.4.2 /
qiskit-aer 0.15.1): Bell 0.197 s, GHZ-10 0.181 s, 27-qubit width 0.196 s,
300-layer Clifford 0.913 s — all within the reported `[0.4t, 3t]` band. The
exponential (statevector/MPS) branch remains a worst-case bound that
overestimates low-entanglement circuits; the `note` field says so.

---

## Plans, quotas & tenant isolation (P1)

Every route under `/v1` is scoped to the authenticated identity. Job records
carry `owner_id` + `entity="job"` (written at create), and the `owner_id-index`
GSI answers tenant queries without a table scan.

### Plan tiers (all ceilings are real, enforced admission checks)

| Limit                              | free    | developer | pro     | enterprise |
|------------------------------------|---------|-----------|---------|------------|
| monthly requests (submit+analyze)  | 200     | 2 000     | 20 000  | 200 000    |
| monthly compute                    | 1 h     | 10 h      | 50 h    | 500 h      |
| concurrent queued/running jobs     | 2       | 8         | 32      | 128        |
| max qubits per submitted circuit   | 12      | 20        | 28      | 28         |
| max shots per run                  | 2 048   | 4 096     | 8 192   | 8 192      |
| requests per minute per identity   | 60      | 180       | 600     | 1 200      |

Registration always lands on `free` (`QUAC_DEFAULT_PLAN` overrides locally
only). Tiers live in `api/plans.py`; the plan a user row names resolves via
`get_plan()` which falls **down** to `free` for unknown tiers — never up.

### Submission gate order (`POST /v1/quantum/simulate`)

1. **firewall** → `422` (banned syntax, 28-qubit platform cap, size ceilings —
   zero infra writes);
2. **plan size** → `403 PLAN_LIMIT_EXCEEDED` (`observed_qubits` vs
   `plan_max_qubits`, or shots vs `plan_max_shots`);
3. **compute admission** → `429 QUOTA_EXCEEDED (quota: compute_ms)` against the
   current month's recorded worker time;
4. **concurrency** → `429 TOO_MANY_ACTIVE_JOBS` (owner's queued+processing count;
   the GSI is eventually consistent, so this cap is soft by one — documented);
5. **monthly request counter** → `429 QUOTA_EXCEEDED (quota: requests)` via an
   atomic conditional `ADD` that cannot over-admit under concurrency;
6. persist (`owner_id`, `entity`) → enqueue (`202`).

`POST /v1/quantum/analyze` counts against the monthly **request** quota only —
deliberately no plan size/shot ceilings, because pre-flight analysis exists to
inspect big circuits before you run them. Rate limits (`429 RATE_LIMIT_EXCEEDED`
+ `Retry-After`) apply to both identities: the global per-IP window
(`QUAC_RATE_LIMIT_REQUESTS/WINDOW`, default 120/60 s) and the per-plan
per-minute window.

Usage counters are read back from `GET /v1/auth/me` (`usage.requests`,
`usage.compute_ms` for the current UTC month). The API increments `requests`
at admission; the worker `ADD`s `compute_ms` after each terminal execution.

### Owner-scoped job listing

```bash
curl -sS "http://localhost:8000/v1/quantum/jobs?status=completed&limit=20" \
  -H "X-API-Key: $QUAC_KEY"
```

* newest first, **cursor-paginated**: response is
  `{"jobs": [...], "next_cursor": "<opaque base64>|null"}`; pass `cursor=…` for
  the next page (malformed cursor → `400 INVALID_CURSOR`, never a 500);
* `status` filters to `queued|processing|completed|failed|cancelled`
  (invalid value → `422`); `limit` is clamped to 1-100 server-side;
* rows expose `job_id, status, created_at, completed_at, format, shots,
  qubit_count, error_message` — never another tenant's data (see below).

### Tenant isolation guarantees

* reading/cancelling **someone else's** job id returns the same
  `404 JOB_NOT_FOUND` envelope as a nonexistent id (message echoes only the
  id you asked for) — existence never leaks;
* listings are disjoint: a `USER#`/`KEY#` row sharing your `owner_id` can never
  appear in `GET /v1/quantum/jobs` (the `entity` discriminator filters it);
* cross-tenant key revoke → `404`, and your key keeps working;
* the e2e driver proves each of these against the real stack with two accounts.

### Legacy-row migration

Records written before P1 (no `entity`/`owner_id`/`schema_version`) are
backfilled by:

```bash
python scripts/migrate_ownership.py --dry-run   # report only
python scripts/migrate_ownership.py             # SET entity="job", owner if absent → "system"
```

Idempotent (conditional on `attribute_not_exists`), `--owner <username>` bills a
specific tenant instead of `system`, and the e2e run verifies dry-run → update →
no-op-rerun against the live table. Such rows stay invisible to every user's
listing (owner `system`).

---

## Web console

`web/` is a React + Vite + TypeScript + Tailwind console in front of the same
API the CLI uses — same origin, no separate backend, no CORS. Every page except
`/login` and `/register` requires a session (see above). During development the
Vite dev server proxies `/v1` and `/healthz` to `localhost:8000`
(`web/vite.config.ts`); the console does not yet ship its own container
(nginx serving + the compose `web` service are P7.15).

### Run it locally

```bash
docker compose up -d localstack api worker   # API on localhost:8000
cd web
npm ci
npm run dev                                   # console on localhost:5173
```

Open `http://localhost:5173` — anonymous visitors are redirected to `/login`;
register a free account (or sign in with one you created earlier) and the
console boots your session, plan ceilings and job list.

`npm run build` type-checks (`tsc -b`) and bundles for production; the CI
`web` job runs exactly `npm ci` + `npm run build`.

The API types in `web/src/api/schema.ts` are generated from the live FastAPI
spec — regenerate after any contract change and commit the result:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.ts
```

### Pages and what each one does

| Route            | Page         | Functions                                                                     |
|------------------|--------------|-------------------------------------------------------------------------------|
| `/login`         | **AuthPage** | username/password sign-in (`POST /v1/auth/login`), link to register, deep-link redirect back to the originally requested route, real API errors (401/422) rendered from the envelope |
| `/register`      | **AuthPage** | account creation (`POST /v1/auth/register` → free plan, session cookie set by the API), username/password rule hints, `409 USERNAME_TAKEN` handling |
| `/`              | **Overview** | live API health (`GET /healthz`), platform limits (`GET /v1/quantum/formats`), **Account & usage** card (plan ceilings + this month's `requests`/`compute_ms` from `GET /v1/auth/me`), five newest owner-scoped jobs (`GET /v1/quantum/jobs?limit=5`) |
| `/studio`        | **Studio**   | circuit editor (OpenQASM 3 / Qiskit JSON / Cirq JSON), debounced pre-flight analysis (`POST /v1/quantum/analyze`: size, depth, estimated runtime/memory, backend recommendation + reason), run submission (`POST /v1/quantum/simulate`) with a fresh idempotency key — after success the server list is invalidated and the job page opens, structured firewall errors rendered with line/column hints |
| `/jobs`          | **Jobs**     | server-side listing (`GET /v1/quantum/jobs`): newest-first rows, status filter (`?status=`), cursor pagination ("Load older jobs"), live 5 s refresh while any row is queued/processing, lookup-any-job-by-id box, honest empty states |
| `/jobs/:jobId`   | **JobDetail**| live status polling (2 s while queued/running, stops on terminal states), full result document (counts, exact probabilities, backend, reason, S3 link) once completed, cancel action (`POST .../cancel`) that also refreshes the list |

Session handling: `lib/auth.tsx` boots every load from `GET /v1/auth/me`
(`401` → anonymous, network failure → visible error, never a fake sign-out).
`App.tsx` wraps the shell in `RequireAuth`: anonymous visitors are redirected
to `/login` with the target route preserved; while the boot request is in
flight the console shows "Checking session…" instead of flashing a redirect.
Sign-out clears the session **and drops every cached tenant query**, so a
second account on the same browser starts from an empty cache.

Shared pieces: `Shell.tsx` (nav, identity menu — username, plan badge, sign
out; API status pill from `/healthz`; dark/light/system theme toggle —
dark-first), `ui.tsx` (Card/Badge/Stat/ErrorBanner/Skeleton/EmptyState/…),
`client.ts` (typed fetch, normalises the API's `{detail}` error envelope into
`ApiError`, plus the auth/keys/jobs-list calls), `schema.ts` (generated
OpenAPI types).

### Honest scope notes

* **The editor is a plain monospace textarea** — CodeMirror with syntax
  highlighting, gutter and error markers is P7.4.
* **No unit tests / served container yet** — vitest component tests are
  P7.16; the nginx + compose serving path is P7.15. The CI `web` job is a
  build gate (type-check + bundle).
* **Verified against the real stack in a browser** (not mocked): redirect to
  `/login` when signed out, registration → session → Overview account card,
  Studio submit → job page poll → real Bell counts + S3 download link, the
  Jobs list showing only that account's rows, status-filter empty state,
  sign-out → `/login`, and a second registered account seeing `0` jobs (no
  cache bleed).

---

## Webhook contract

When a `webhook_url` was supplied at submission, Quac POSTs to it on terminal
state:

```json
{
  "event": "simulation.completed",
  "job_id": "…",
  "status": "completed",
  "shots": 1024,
  "method": "stabilizer",
  "backend": "stabilizer",
  "selection_reasons": [
    "all 4 operations are Clifford (depth 3, 2 qubits) -> stabilizer backend, exact simulation with cost polynomial in circuit size"
  ],
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
`download_url` (including dedicated `simulation.timeout` events). A cancelled
job delivers `event: "simulation.cancelled"` with `status: "cancelled"` and a
null `download_url`. Jobs submitted without a `webhook_url` emit no callbacks —
consumers poll the result endpoint instead.

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
  Point-in-Time Recovery enabled, TTL on `expires_at`, SSE on. Every job record
  carries `owner_id` (default `system` until identity lands) and
  `schema_version`; the `owner_id-index` GSI (`owner_id` hash + `created_at`
  range, `ALL` projection) enables tenant-scoped listings without a table scan —
  mirrored by `scripts/localstack_init.py`.
* **SQS** — FIFO with content-based deduplication *and* API-computed
  `MessageDeduplicationId`; DLQ with `maxReceiveCount = 3`; visibility timeout
  660 s > Lambda's 600 s cap; queue depth alarm on the DLQ.
* **S3** — SSE-KMS with a customer-managed key (auto-rotating), all four
  public-access blocks,
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
  For `cirq_json` the width is counted from the distinct qubits the document
  actually references, so a bare `cirq.to_json()` payload is capped by
  reality, not by a declared field.

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

The `e2e` driver walks 13 legs with **two real tenants** (random usernames per
run): API liveness; unauthenticated quantum routes refused (401) while
`/healthz` and `/v1/quantum/formats` stay open; tenant A registers by session
cookie (`201`, free plan, `409` on duplicate) and mints a shown-once API key
(listing never exposes the secret); tenant B registers too; the firewall
rejects a banned circuit pre-queue (422, zero infra writes); A submits a
Bell-state circuit by API key (202 + UUIDv4); **tenant isolation** — B gets the
same `404` for A's job id as for a missing one, cannot cancel it, and the two
listings are disjoint; status polling to `completed`; the
`results/{Y}/{M}/{D}/{job_id}.json` S3 object (counts sum to shots, Bell
correlations only); the signed webhook callback at the sink; the **monthly
usage counters** (`requests` recorded by the API, `compute_ms` settled by the
worker, tenant-scoped); key revocation (revoked key → 401, B's key unaffected);
and the **ownership migration** on a seeded pre-P1 row (dry-run reports it,
update backfills `entity`/`owner`/`schema_version`, the rerun is a no-op, and
the row never leaks into any tenant's listing). Exit code 0 means the entire
path is healthy. `scripts/localstack_init.py` is idempotent and mirrors the
Terraform-provisioned resources exactly.

---

## Continuous integration

`.github/workflows/ci.yml` runs five jobs on every push/PR:

| Job         | Steps                                                                 |
|-------------|-----------------------------------------------------------------------|
| `quality`   | ruff · mypy · pytest · gitleaks (against the pinned prod dependencies)|
| `terraform` | `fmt -check` · `init -backend=false` · `validate`                     |
| `web`       | `npm ci` · `tsc -b` · `vite build` (the console in `web/`)            |
| `security`  | trivy — dep CVEs, IaC/Dockerfile/compose misconfig, leaked secrets    |
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
  Dockerfile / compose misconfigurations. Locally the misconfig + secret
  scanners run offline (currently 0 HIGH/CRITICAL); the CVE scan runs in CI
  (`security` job) where the vulnerability DB downloads in seconds. Three
  reviewed accepted risks are documented in `.trivyignore`: the public ALB
  (it *is* the product surface), 443-only egress via NAT (no VPC endpoints),
  and root execution in the Lambda image (platform design).

---

## Testing

```bash
python -m pytest tests -q    # 294 tests across 15 files
```

| File                            | Covers                                                            |
|---------------------------------|-------------------------------------------------------------------|
| `test_schemas_firewall.py`      | allow/deny matrix, injection vectors, 28-qubit cap (incl. cirq width from bare documents), Pydantic contract |
| `test_queue_dedup.py`           | dedup-id determinism/precedence, envelope round-trip              |
| `test_api_endpoints.py`         | 202 flow, 404s, rate-limit 429 + `Retry-After`, no-side-effect rejections |
| `test_auth.py`                  | P1 identity: registration rules (username/password validation, 409), argon2id hashing, login/logout/me flows, session-cookie lifecycle, generic 401 envelope, credential precedence (API key over cookie) |
| `test_api_keys.py`              | key minting (shown-once secret, peppered HMAC stored), listing never exposes secrets, revocation semantics (401 after revoke, cross-tenant 404) |
| `test_quota.py`                 | plan ceilings per tier (free/developer/pro/enterprise), gate order (403 vs 429), atomic monthly request counter (no over-admission under concurrency), compute-ms admission, concurrency cap, per-plan rate limit + `Retry-After` |
| `test_tenant_isolation.py`      | owner stamped on create, cross-tenant reads/cancels → identical 404, listings disjoint, identity rows (USER#/KEY#) never appear in job listings |
| `test_jobs_list.py`             | owner-scoped listing contract: newest-first, cursor pagination (opaque token, invalid → 400), status filter (invalid → 422), limit clamped 1-100, empty state for new accounts |
| `test_job_results_cancel.py`    | optional webhook, result readiness/artifact mapping, cancel transitions |
| `test_database_records.py`      | record shape (owner/schema), conditional cancel + guarded updates |
| `test_analysis.py`              | exact metrics, basis labels, warnings, backend recommendation + capability matrix, cross-format parity (qasm3/qiskit_json/cirq_json), worker parity, analyze endpoint contract, cirq translator correction |
| `test_backends.py`              | P3 selector: Clifford classification matrix, capability/GPU detection, selection reasoning, typed overrides, Aer execution (stabilizer vs statevector agreement) |
| `test_simulator_factory.py`     | format converters, depth-rule selection, Aer execution via the selected backend |
| `test_worker_webhook.py`        | 3-attempt backoff, HMAC signing, SIGALRM budget, batch reporting  |
| `test_worker_cancellation.py`   | pre-execution cancel/duplicate guards, finalisation race, acks    |

Qiskit-dependent modules skip cleanly (`pytest.importorskip`) when
`qiskit-aer` is absent, cirq tests skip when `cirq-core` is absent, and
SIGALRM tests skip on non-POSIX hosts (Lambda is Linux).

## Security posture at a glance

* Non-root container (`uid 10001`), no compilers/packages in runtime images.
* Webhook URLs: strict http/https, credentials-in-URL rejected.
* HMAC-SHA256 signed callbacks; TLS-only bucket/queue policies; SSE on S3,
  SQS and DynamoDB; ECR image scanning + immutable tags; ALB TLS 1.3 policy.
* Identity (P1): passwords hashed with **argon2id** peppered with
  `QUAC_SECRET_KEY` (sensitive terraform var, never committed); session tokens
  and API-key secrets stored only as peppered SHA-256/HMAC hashes — the raw
  `qk_…` secret exists exactly once, in the create response; sessions expire
  (24 h TTL, revoked on logout); auth events recorded as `AUDIT#` rows
  (400-day TTL); unknown logins verify against a dummy hash and all failures
  return one generic `401 UNAUTHENTICATED` (no username oracle).
* Least-privilege IAM scoped to workspace ARNs; DLQ + CloudWatch alarms for
  error-rate and poison messages.
