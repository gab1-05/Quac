/**
 * Studio — the core workflow: edit a circuit, watch the live pre-flight
 * report, then run it on the selector-chosen backend.
 *
 * The stats strip comes from `POST /v1/quantum/analyze` (synchronous, zero
 * side effects) debounced while typing; "Run simulation" calls
 * `POST /v1/quantum/simulate` with a fresh idempotency key, records the job in
 * the browser-local history, and navigates to the job page for live tracking.
 */
import { useMutation, useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  analyzeCircuit,
  ApiError,
  submitSimulation,
  type CircuitFormat,
  type JobStatus,
} from '../api/client'
import { Badge, Card, Disclosure, ErrorBanner, Skeleton, Stat } from '../components/ui'
import { useDebouncedValue } from '../lib/hooks'
import { formatBytes, formatInteger, formatSecondsBand, formatUsdBand } from '../lib/format'
import { recordJob } from '../lib/history'

const DEFAULT_QASM = `OPENQASM 3.0;
include "stdgates.inc";
qubit[2] q;
bit[2] c;
h q[0];
cx q[0], q[1];
c = measure q;
`

const FORMAT_OPTIONS: { value: CircuitFormat; label: string; hint: string }[] = [
  { value: 'qasm3', label: 'OpenQASM 3.0', hint: 'text source, edited below' },
  { value: 'qiskit_json', label: 'Qiskit JSON', hint: 'paste a serialized Qiskit circuit document' },
  { value: 'cirq_json', label: 'Cirq JSON', hint: 'paste a serialized Cirq circuit document' },
]

/** Tone for a firewall/analysis warning severity. */
function warningTone(severity: 'info' | 'warning' | 'opportunity'): 'accent' | 'warn' | 'ok' {
  if (severity === 'info') return 'accent'
  if (severity === 'warning') return 'warn'
  return 'ok'
}

export default function Studio() {
  const navigate = useNavigate()
  const [format, setFormat] = useState<CircuitFormat>('qasm3')
  const [circuitText, setCircuitText] = useState(DEFAULT_QASM)
  const [shots, setShots] = useState(1024)

  // Debounce the analyze call while typing; keep the previous report visible.
  const debouncedText = useDebouncedValue(circuitText, 600)
  const trimmedText = debouncedText.trim()
  const parsedJson = useMemo(() => {
    if (format === 'qasm3') return { data: trimmedText as string | Record<string, unknown>, error: null as string | null }
    try {
      const value: unknown = JSON.parse(trimmedText)
      if (typeof value !== 'object' || value === null || Array.isArray(value)) {
        return { data: null, error: 'serialized circuit documents must be a JSON object' }
      }
      return { data: value as Record<string, unknown>, error: null }
    } catch (error) {
      return { data: null, error: error instanceof Error ? error.message : 'invalid JSON' }
    }
  }, [format, trimmedText])

  const payload = useMemo(
    () =>
      parsedJson.data === null
        ? null
        : ({ circuit_data: parsedJson.data, format, shots } as const),
    [parsedJson.data, format, shots],
  )

  const analysis = useQuery({
    queryKey: ['analyze', format, trimmedText, shots],
    enabled: payload !== null && trimmedText.length > 0,
    queryFn: () => analyzeCircuit(payload!),
    staleTime: 30_000,
    placeholderData: (previous) => previous,
  })

  const submit = useMutation({
    mutationFn: () =>
      submitSimulation({
        circuit_data: parsedJson.data!,
        format,
        shots,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: (accepted) => {
      recordJob({
        job_id: accepted.job_id,
        submitted_at: new Date().toISOString(),
        format,
        shots,
        last_known_status: accepted.status as JobStatus,
      })
      navigate(`/jobs/${accepted.job_id}`)
    },
  })

  const report = analysis.data
  const runDisabled = payload === null || submit.isPending

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-bold text-ink-strong">Circuit Studio</h1>
        <p className="mt-1 text-sm text-ink-muted">
          The pre-flight report below is computed live by the same analyzer the worker uses —
          nothing runs until you press <span className="text-ink">Run simulation</span>.
        </p>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* ---- Editor ---------------------------------------------------- */}
        <Card title="Circuit">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-ink-muted">
              Format
              <select
                value={format}
                onChange={(event) => setFormat(event.target.value as CircuitFormat)}
                className="rounded-lg border border-line bg-surface-2 px-2 py-1.5 text-sm text-ink"
              >
                {FORMAT_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-ink-muted">
              Shots
              <input
                type="number"
                min={1}
                max={8192}
                value={shots}
                onChange={(event) => setShots(Math.max(1, Number(event.target.value) || 1))}
                className="w-28 rounded-lg border border-line bg-surface-2 px-2 py-1.5 font-mono text-sm text-ink"
              />
            </label>
            <span className="ml-auto text-xs text-ink-muted">
              {FORMAT_OPTIONS.find((option) => option.value === format)?.hint}
            </span>
          </div>

          <label className="mt-3 block text-xs text-ink-muted" htmlFor="circuit-editor">
            Circuit source
          </label>
          <textarea
            id="circuit-editor"
            value={circuitText}
            onChange={(event) => {
              setCircuitText(event.target.value)
              submit.reset()
            }}
            spellCheck={false}
            rows={14}
            className="mt-1 w-full resize-y rounded-lg border border-line bg-surface-2 p-3 font-mono text-sm text-ink"
          />

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => submit.mutate()}
              disabled={runDisabled}
              className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-accent-ink disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submit.isPending ? 'Submitting…' : 'Run simulation'}
            </button>
            <button
              type="button"
              onClick={() => {
                setFormat('qasm3')
                setShots(1024)
                setCircuitText(DEFAULT_QASM)
                submit.reset()
              }}
              className="rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-muted hover:text-ink"
            >
              Reset to Bell pair
            </button>
            {payload === null && trimmedText.length > 0 && (
              <span className="text-xs text-warn">
                Not runnable yet: {parsedJson.error ?? 'invalid circuit document'}
              </span>
            )}
          </div>
          <ErrorBanner error={submit.error} title="Submission rejected" />
        </Card>

        {/* ---- Pre-flight report ---------------------------------------- */}
        <Card title="Pre-flight report" actions={analysis.isFetching ? <span className="text-xs text-ink-muted">analysing…</span> : null}>
          {parsedJson.error && trimmedText.length > 0 && (
            <p className="text-sm text-warn">Waiting for a valid circuit document: {parsedJson.error}</p>
          )}
          {trimmedText.length === 0 && (
            <p className="text-sm text-ink-muted">Enter a circuit to see its analysis.</p>
          )}
          {analysis.isLoading && <Skeleton className="h-40 w-full" />}
          <ErrorBanner
            error={analysis.error}
            title={analysis.error instanceof ApiError && analysis.error.status === 422 ? 'Circuit rejected by the firewall' : 'Analysis failed'}
          />
          {report && (
            <div className="flex flex-col gap-4">
              {/* Structural stats */}
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                <Stat label="Qubits" value={report.num_qubits} />
                <Stat label="Classical bits" value={report.num_clbits} />
                <Stat label="Depth" value={report.depth} />
                <Stat label="Operations" value={report.total_operations} />
                <Stat
                  label="2-qubit density"
                  value={`${(report.two_qubit_density * 100).toFixed(0)}%`}
                  hint="share of ≥2q ops"
                />
                <Stat
                  label="Executable"
                  value={report.executable ? 'yes' : 'no'}
                  hint={report.executable ? undefined : 'see warnings'}
                />
              </div>

              {/* Backend recommendation */}
              <div className="rounded-lg border border-line bg-surface-2 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                    Recommended backend
                  </span>
                  <Badge tone="accent">{report.recommendation.backend}</Badge>
                  <Badge>{report.recommendation.method}</Badge>
                </div>
                <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-ink">
                  {report.recommendation.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ol>
                {report.recommendation.caveats.length > 0 && (
                  <ul className="mt-2 space-y-1 text-xs text-warn">
                    {report.recommendation.caveats.map((caveat) => (
                      <li key={caveat}>⚠ {caveat}</li>
                    ))}
                  </ul>
                )}
                <Disclosure summary="Backend capability matrix">
                  <table className="w-full text-left text-xs">
                    <thead>
                      <tr className="text-ink-muted">
                        <th className="py-1 pr-2 font-medium">Backend</th>
                        <th className="py-1 pr-2 font-medium">Device</th>
                        <th className="py-1 pr-2 font-medium">Max qubits</th>
                        <th className="py-1 font-medium">Availability</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.values(report.backends).map((backend) => (
                        <tr key={backend.name} className="border-t border-line">
                          <td className="py-1 pr-2 font-mono">{backend.name}</td>
                          <td className="py-1 pr-2">{backend.device}</td>
                          <td className="py-1 pr-2">{backend.max_qubits || '—'}</td>
                          <td className="py-1">
                            {backend.available ? (
                              <span className="text-ok">available</span>
                            ) : (
                              <span className="text-ink-muted">
                                {backend.unavailable_reason ?? 'unavailable'}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Disclosure>
              </div>

              {/* Estimates */}
              <div className="grid gap-2 sm:grid-cols-3">
                <Stat
                  label="Runtime (est.)"
                  value={formatSecondsBand(report.estimates.runtime_seconds)}
                  hint="worst-case planning band"
                />
                <Stat
                  label="Statevector memory"
                  value={formatBytes(report.estimates.statevector_memory_bytes.value)}
                  hint="exact"
                />
                <Stat
                  label="Compute cost (est.)"
                  value={formatUsdBand(report.estimates.compute_cost_usd)}
                  hint="depends on shots/backend"
                />
              </div>
              <Disclosure summary="How these numbers are derived">
                <p className="font-mono">{report.estimates.runtime_seconds.note}</p>
              </Disclosure>

              {/* Warnings */}
              {report.warnings.length > 0 && (
                <ul className="flex flex-col gap-1.5">
                  {report.warnings.map((warning) => (
                    <li key={`${warning.code}-${warning.message}`}>
                      <Badge tone={warningTone(warning.severity)}>{warning.code}</Badge>{' '}
                      <span className="text-sm text-ink">{warning.message}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </Card>
      </div>

      {/* Reference strip: what a run will cost in API terms */}
      <p className="text-xs text-ink-muted">
        Runs are asynchronous: the console submits with an idempotency key, the worker picks a
        backend through the selector, and the job page polls{' '}
        <code className="font-mono">GET /v1/quantum/jobs/&#123;id&#125;</code> until a terminal
        state. Estimated bands above are planning guidance, not benchmarks —{' '}
        {formatInteger(shots)} shots are requested for the next run.
      </p>
    </div>
  )
}
