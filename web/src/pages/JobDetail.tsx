/**
 * JobDetail — track one job to its terminal state and show the result.
 *
 * Polls `GET /v1/quantum/jobs/{id}` every 2 s while the job is queued or
 * processing (immediately stops on any terminal state), then fetches the
 * result document from `/result` when the job completed. Cancellation goes
 * through the guarded `POST .../cancel` endpoint; the API's conditional
 * update decides whether it lands (a job that already finished stays
 * finished).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ApiError,
  cancelJob,
  getJob,
  getJobResult,
  STATUS_LABEL,
  TERMINAL_STATUSES,
  type JobStatus,
} from '../api/client'
import { Badge, Card, Disclosure, EmptyState, ErrorBanner, Skeleton, Stat } from '../components/ui'
import { formatDuration, formatInteger, formatTimestamp } from '../lib/format'
import { updateJobStatus } from '../lib/history'

const STATUS_TONE: Record<JobStatus, 'ok' | 'warn' | 'err' | 'accent' | 'neutral'> = {
  queued: 'neutral',
  processing: 'accent',
  completed: 'ok',
  failed: 'err',
  cancelled: 'warn',
}

/** Lifecycle rail: queued → processing → terminal. */
function StatusTimeline({ status }: { status: JobStatus }) {
  const reached = (step: JobStatus): boolean => {
    const order: JobStatus[] = ['queued', 'processing', 'completed']
    if (step === 'completed') return TERMINAL_STATUSES.has(status)
    return order.indexOf(status) >= order.indexOf(step) || TERMINAL_STATUSES.has(status)
  }
  return (
    <ol className="flex flex-wrap items-center gap-2 text-xs" aria-label="Job lifecycle">
      {(['queued', 'processing', 'completed'] as const).map((step) => (
        <li key={step} className="flex items-center gap-2">
          <span
            className={`rounded-full border px-2 py-0.5 font-medium ${
              reached(step)
                ? 'border-accent/50 bg-accent/10 text-accent'
                : 'border-line bg-surface-2 text-ink-muted'
            }`}
          >
            {STATUS_LABEL[step]}
          </span>
          {step !== 'completed' && <span className="text-ink-muted" aria-hidden="true">→</span>}
        </li>
      ))}
      {!reached('completed') && (
        <li>
          <Badge tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Badge>
        </li>
      )}
    </ol>
  )
}

export default function JobDetail() {
  const { jobId = '' } = useParams()
  const queryClient = useQueryClient()

  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => getJob(jobId),
    enabled: jobId !== '',
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && !TERMINAL_STATUSES.has(status) ? 2_000 : false
    },
  })

  const result = useQuery({
    queryKey: ['job-result', jobId],
    queryFn: () => getJobResult(jobId),
    enabled: job.data?.status === 'completed',
  })

  // Keep the browser-local history in sync with observed states.
  useEffect(() => {
    if (job.data) updateJobStatus(job.data.job_id, job.data.status)
  }, [job.data])

  const cancel = useMutation({
    mutationFn: () => cancelJob(jobId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['job', jobId] }),
  })

  if (job.isError) {
    const notFound = job.error instanceof ApiError && job.error.status === 404
    return notFound ? (
      <EmptyState
        title="Job not found"
        body={
          <>
            The API has no record for <code className="font-mono">{jobId}</code>. Check the id, or
            submit a new circuit from the{' '}
            <Link to="/studio" className="text-accent hover:underline">
              Studio
            </Link>
            .
          </>
        }
      />
    ) : (
      <ErrorBanner error={job.error} title="Cannot load this job" />
    )
  }

  if (job.isLoading || !job.data) return <Skeleton className="h-64 w-full" />

  const status = job.data.status
  const terminal = TERMINAL_STATUSES.has(status)
  const payload = result.data?.result ?? null

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0">
          <h1 className="truncate font-mono text-lg font-bold text-ink-strong" title={job.data.job_id}>
            {job.data.job_id}
          </h1>
          <p className="text-xs text-ink-muted">
            created {formatTimestamp(job.data.created_at)}
            {job.data.completed_at &&
              ` · finished in ${formatDuration(job.data.created_at, job.data.completed_at)}`}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {!terminal && (
            <button
              type="button"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
              className="rounded-lg border border-err/50 bg-err/10 px-3 py-1.5 text-sm font-medium text-err hover:bg-err/20 disabled:opacity-50"
            >
              {cancel.isPending ? 'Cancelling…' : 'Cancel job'}
            </button>
          )}
          <Link
            to="/jobs"
            className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-sm text-ink-muted hover:text-ink"
          >
            All jobs
          </Link>
        </div>
      </div>

      <Card title="Status">
        <StatusTimeline status={status} />
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          <Badge tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Badge>
          {!terminal && (
            <span className="text-xs text-ink-muted" aria-live="polite">
              polling every 2 s — stops automatically once finished
            </span>
          )}
        </div>
        <ErrorBanner error={cancel.error} title="Cancellation failed" />
        {status === 'failed' && job.data.error_message && (
          <div className="mt-3 rounded-lg border border-err/40 bg-err/10 px-3 py-2 text-sm text-err">
            {job.data.error_message}
          </div>
        )}
        {status === 'cancelled' && (
          <p className="mt-3 text-sm text-ink-muted">
            Cancelled — a running simulation stops at its next checkpoint.
          </p>
        )}
      </Card>

      {status === 'completed' && (
        <Card
          title="Result"
          actions={
            result.data?.download_url ? (
              <a
                href={result.data.download_url}
                className="text-xs font-medium text-accent hover:underline"
                target="_blank"
                rel="noreferrer"
              >
                Download full document ({result.data.download_expires_in_seconds}s link) ↗
              </a>
            ) : null
          }
        >
          {result.isLoading && <Skeleton className="h-32 w-full" />}
          <ErrorBanner error={result.error} title="Cannot load the result" />
          {payload && (
            <div className="flex flex-col gap-4">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Stat label="Backend" value={payload.backend || payload.method} hint="selector choice" />
                <Stat label="Method" value={payload.method} hint="Aer method used" />
                <Stat label="Shots" value={formatInteger(payload.shots)} />
                <Stat
                  label="Most frequent"
                  value={payload.most_frequent ?? '—'}
                  hint={`${payload.num_qubits} qubits · depth ${payload.depth}`}
                />
              </div>

              {payload.selection_reasons && payload.selection_reasons.length > 0 && (
                <div className="rounded-lg border border-line bg-surface-2 p-3">
                  <div className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
                    Why this backend
                  </div>
                  <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-ink">
                    {payload.selection_reasons.map((reason) => (
                      <li key={reason}>{reason}</li>
                    ))}
                  </ul>
                </div>
              )}

              <div>
                <h3 className="mb-2 text-sm font-semibold text-ink-strong">Counts</h3>
                <CountsTable counts={payload.counts} totalShots={payload.shots} />
              </div>

              {payload.statevector_probabilities && (
                <div>
                  <h3 className="mb-2 text-sm font-semibold text-ink-strong">
                    Exact probabilities (statevector inlined)
                  </h3>
                  <CountsTable
                    counts={payload.statevector_probabilities}
                    totalShots={0}
                    percentMode
                  />
                </div>
              )}

              {payload.metadata && Object.keys(payload.metadata).length > 0 && (
                <Disclosure summary="Reproducibility metadata">
                  <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-xs">
                    {JSON.stringify(payload.metadata, null, 2)}
                  </pre>
                </Disclosure>
              )}
            </div>
          )}
        </Card>
      )}
    </div>
  )
}

/** Bitstring histogram with proportional bars (or exact percentages). */
function CountsTable({
  counts,
  totalShots,
  percentMode = false,
}: {
  counts: Record<string, number>
  totalShots: number
  percentMode?: boolean
}) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) return <p className="text-sm text-ink-muted">No outcomes recorded.</p>
  const max = entries[0][1]
  return (
    <table className="w-full text-left text-sm">
      <thead>
        <tr className="text-xs text-ink-muted">
          <th className="py-1 font-medium">Outcome</th>
          <th className="py-1 font-medium">{percentMode ? 'Probability' : 'Count'}</th>
          <th className="w-1/2 py-1 font-medium" aria-hidden="true" />
        </tr>
      </thead>
      <tbody>
        {entries.map(([outcome, value]) => {
          const share = totalShots > 0 ? (value / totalShots) * 100 : null
          return (
            <tr key={outcome} className="border-t border-line">
              <td className="py-1 pr-3 font-mono text-ink-strong">{outcome}</td>
              <td className="py-1 pr-3 font-mono text-ink">
                {percentMode ? `${(value * 100).toFixed(2)}%` : formatInteger(value)}
                {share !== null && <span className="text-ink-muted"> · {share.toFixed(1)}%</span>}
              </td>
              <td className="py-1">
                <div className="h-2 rounded bg-surface-2">
                  <div
                    className="h-2 rounded bg-accent/70"
                    style={{ width: `${Math.round((value / max) * 100)}%` }}
                  />
                </div>
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
