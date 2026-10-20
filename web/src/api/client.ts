/**
 * Typed HTTP client for the Quac API.
 *
 * Every call is typed against the generated OpenAPI schema in `./schema.ts`
 * (regenerate while the compose stack is up:
 * `npx openapi-typescript http://localhost:8000/openapi.json -o src/api/schema.ts`).
 *
 * Errors are normalised into {@link ApiError} so pages can render the API's
 * `{"detail": ...}` envelope — including the firewall's structured 422
 * rejections (`error`, `message`, `line`) — without re-parsing JSON per call.
 *
 * The client uses same-origin relative paths: the Vite dev server proxies
 * `/v1` and `/healthz` to the compose API, and production serves the built
 * assets behind the same origin.
 */
import type { components } from './schema'

export type AnalysisReport = components['schemas']['CircuitAnalysisResponse']
export type AnalysisRecommendation = components['schemas']['AnalysisRecommendation']
export type AnalysisWarning = components['schemas']['AnalysisWarning']
export type BackendCapability = components['schemas']['BackendCapability']
export type CircuitFormat = components['schemas']['CircuitFormat']
export type JobCancelResponse = components['schemas']['JobCancelResponse']
export type JobResultPayload = components['schemas']['JobResultPayload']
export type JobResultResponse = components['schemas']['JobResultResponse']
export type JobStatus = components['schemas']['JobStatus']
export type JobStatusResponse = components['schemas']['JobStatusResponse']
export type SimulationAcceptedResponse = components['schemas']['SimulationAcceptedResponse']

/** `GET /healthz` body (the OpenAPI schema types this as a free-form record). */
export interface HealthResponse {
  status: string
  service: string
  version: string
}

/** `GET /v1/quantum/formats` body (free-form record in the schema). */
export interface FormatsResponse {
  formats: CircuitFormat[]
  max_qubits: number
  max_shots: number
}

/** Shared request body of `POST /v1/quantum/analyze` and `POST /v1/quantum/simulate`. */
export interface CircuitPayload {
  circuit_data: string | Record<string, unknown>
  format: CircuitFormat
  shots: number
}

/** `POST /v1/quantum/simulate` body — ingestion requires an idempotency key. */
export interface SubmitPayload extends CircuitPayload {
  idempotency_key: string
  webhook_url?: string
}

/** Normalised API error: HTTP status + the envelope's machine-readable fields. */
export class ApiError extends Error {
  readonly status: number
  /** Firewall error code (`QASM_PARSE_ERROR`, `QUBIT_LIMIT_EXCEEDED`, …) when present. */
  readonly code: string | null
  /** 1-based source line the rejection points at, when present. */
  readonly line: number | null

  constructor(
    status: number,
    message: string,
    code: string | null = null,
    line: number | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.line = line
  }
}

/** Job states that never transition again — stop polling on these. */
export const TERMINAL_STATUSES: ReadonlySet<JobStatus> = new Set([
  'completed',
  'failed',
  'cancelled',
])

/** Human-readable labels for job lifecycle states. */
export const STATUS_LABEL: Record<JobStatus, string> = {
  queued: 'Queued',
  processing: 'Processing',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** Turn a non-2xx response body (`{"detail": ...}`) into an {@link ApiError}. */
async function toApiError(response: Response): Promise<ApiError> {
  let detail: unknown = null
  try {
    const body: unknown = await response.json()
    if (isRecord(body)) detail = body.detail
  } catch {
    // Non-JSON body — fall through to the generic status message.
  }
  if (typeof detail === 'string') {
    return new ApiError(response.status, detail)
  }
  if (isRecord(detail) && typeof detail.message === 'string') {
    const code = typeof detail.error === 'string' ? detail.error : null
    const line = typeof detail.line === 'number' ? detail.line : null
    return new ApiError(response.status, detail.message, code, line)
  }
  if (Array.isArray(detail)) {
    // FastAPI request-validation errors: [{loc, msg, type}, ...]
    const messages = detail
      .filter(isRecord)
      .map((issue) => (typeof issue.msg === 'string' ? issue.msg : JSON.stringify(issue)))
    return new ApiError(
      response.status,
      messages.join('; ') || 'request validation failed',
    )
  }
  return new ApiError(response.status, `request failed with status ${response.status}`)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${path}`, {
      ...init,
      headers: { 'content-type': 'application/json', ...(init.headers ?? {}) },
    })
  } catch {
    throw new ApiError(0, 'cannot reach the API — is the backend running?')
  }
  if (!response.ok) throw await toApiError(response)
  return (await response.json()) as T
}

/** `GET /healthz` — liveness + build version. */
export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/healthz')
}

/** `GET /v1/quantum/formats` — accepted formats and platform limits. */
export function getFormats(): Promise<FormatsResponse> {
  return request<FormatsResponse>('/v1/quantum/formats')
}

/** `POST /v1/quantum/analyze` — synchronous pre-flight report, zero side effects. */
export function analyzeCircuit(payload: CircuitPayload): Promise<AnalysisReport> {
  return request<AnalysisReport>('/v1/quantum/analyze', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** `POST /v1/quantum/simulate` — validate, persist and enqueue (202 Accepted). */
export function submitSimulation(payload: SubmitPayload): Promise<SimulationAcceptedResponse> {
  return request<SimulationAcceptedResponse>('/v1/quantum/simulate', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** `GET /v1/quantum/jobs/{id}` — job lifecycle state. */
export function getJob(jobId: string): Promise<JobStatusResponse> {
  return request<JobStatusResponse>(`/v1/quantum/jobs/${encodeURIComponent(jobId)}`)
}

/** `GET /v1/quantum/jobs/{id}/result` — result document + pre-signed download. */
export function getJobResult(jobId: string): Promise<JobResultResponse> {
  return request<JobResultResponse>(`/v1/quantum/jobs/${encodeURIComponent(jobId)}/result`)
}

/** `POST /v1/quantum/jobs/{id}/cancel` — idempotent cancellation request. */
export function cancelJob(jobId: string): Promise<JobCancelResponse> {
  return request<JobCancelResponse>(`/v1/quantum/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  })
}
