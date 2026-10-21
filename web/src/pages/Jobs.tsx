/**
 * Jobs — the owner-scoped server listing (`GET /v1/quantum/jobs`).
 *
 * Everything here comes from the API: newest-first rows, cursor pagination
 * (opaque `next_cursor`), optional status filter, and a live refresh while
 * any listed job is still queued/processing. The API scopes the list to the
 * signed-in identity, so what renders is exactly what the caller owns — the
 * page never synthesises rows locally.
 *
 * The id-lookup box complements the list: paste any job id you know to open
 * it directly (cross-tenant ids honestly come back 404 from the API).
 */
import { useInfiniteQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  listJobs,
  STATUS_LABEL,
  TERMINAL_STATUSES,
  type JobStatus,
} from '../api/client'
import { Badge, Card, EmptyState, ErrorBanner, Skeleton } from '../components/ui'
import { formatInteger, formatTimestamp } from '../lib/format'

const STATUS_TONE: Record<JobStatus, 'ok' | 'warn' | 'err' | 'accent' | 'neutral'> = {
  queued: 'neutral',
  processing: 'accent',
  completed: 'ok',
  failed: 'err',
  cancelled: 'warn',
}

const STATUS_FILTERS: { value: JobStatus | ''; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'queued', label: 'Queued' },
  { value: 'processing', label: 'Processing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'cancelled', label: 'Cancelled' },
]

const PAGE_SIZE = 20

export default function Jobs() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<JobStatus | ''>('')
  const [lookup, setLookup] = useState('')

  const jobs = useInfiniteQuery({
    queryKey: ['jobs', status],
    queryFn: ({ pageParam }) =>
      listJobs({ status: status === '' ? null : status, limit: PAGE_SIZE, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // Poll while anything on screen can still change; stop once all terminal.
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? []
      const hasActive = pages.some((page) =>
        page.jobs.some((job) => !TERMINAL_STATUSES.has(job.status)),
      )
      return hasActive ? 5_000 : false
    },
  })

  const rows = jobs.data?.pages.flatMap((page) => page.jobs) ?? []

  function openLookup(event: FormEvent) {
    event.preventDefault()
    const id = lookup.trim()
    if (id) navigate(`/jobs/${encodeURIComponent(id)}`)
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-bold text-ink-strong">Jobs</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Your submissions, newest first — served by{' '}
          <code className="font-mono">GET /v1/quantum/jobs</code> and scoped to your account.
        </p>
      </div>

      {/* ---- Lookup + filter ------------------------------------------------ */}
      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <form onSubmit={openLookup} className="flex flex-1 flex-wrap items-end gap-2">
            <label className="flex min-w-52 flex-1 flex-col gap-1 text-xs text-ink-muted">
              Open a job by id
              <input
                value={lookup}
                onChange={(event) => setLookup(event.target.value)}
                placeholder="e.g. 6f1c8a9e-…"
                spellCheck={false}
                className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 font-mono text-sm text-ink focus:border-accent focus:outline-none"
              />
            </label>
            <button
              type="submit"
              disabled={lookup.trim().length === 0}
              className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-sm text-ink-muted hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
            >
              Open
            </button>
          </form>

          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Status filter
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value as JobStatus | '')}
              className="rounded-lg border border-line bg-surface-2 px-2 py-1.5 text-sm text-ink"
            >
              {STATUS_FILTERS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </Card>

      {/* ---- Listing -------------------------------------------------------- */}
      <Card
        title={status ? `${STATUS_LABEL[status]} jobs` : 'All jobs'}
        actions={
          <span className="text-xs text-ink-muted">
            {jobs.isLoading ? 'loading…' : `${formatInteger(rows.length)} shown`}
          </span>
        }
      >
        <ErrorBanner error={jobs.error} title="Cannot load your jobs" />
        {jobs.isLoading && <Skeleton className="h-40 w-full" />}

        {jobs.isSuccess && rows.length === 0 && (
          <EmptyState
            title={status ? `No ${STATUS_LABEL[status].toLowerCase()} jobs` : 'No jobs yet'}
            body={
              status ? (
                <>
                  Nothing in this state right now — clear the filter to see the rest of your
                  submissions.
                </>
              ) : (
                <>
                  Submit a circuit from the{' '}
                  <Link to="/studio" className="text-accent hover:underline">
                    Studio
                  </Link>{' '}
                  and it will show up here with live status.
                </>
              )
            }
            action={
              status ? (
                <button
                  type="button"
                  onClick={() => setStatus('')}
                  className="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-sm text-ink-muted hover:text-ink"
                >
                  Clear filter
                </button>
              ) : null
            }
          />
        )}

        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="text-xs text-ink-muted">
                  <th className="py-1 pr-3 font-medium">Status</th>
                  <th className="py-1 pr-3 font-medium">Job</th>
                  <th className="py-1 pr-3 font-medium">Submitted</th>
                  <th className="py-1 pr-3 font-medium">Circuit</th>
                  <th className="py-1 font-medium">Failure</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((job) => (
                  <tr key={job.job_id} className="border-t border-line align-top">
                    <td className="py-2 pr-3">
                      <Badge tone={STATUS_TONE[job.status]}>{STATUS_LABEL[job.status]}</Badge>
                    </td>
                    <td className="max-w-56 py-2 pr-3">
                      <Link
                        to={`/jobs/${job.job_id}`}
                        className="block truncate font-mono text-xs text-accent hover:underline"
                        title={job.job_id}
                      >
                        {job.job_id}
                      </Link>
                      {job.completed_at && (
                        <span className="text-[11px] text-ink-muted">
                          finished {formatTimestamp(job.completed_at)}
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap py-2 pr-3 text-xs text-ink-muted">
                      {formatTimestamp(job.created_at)}
                    </td>
                    <td className="whitespace-nowrap py-2 pr-3 text-xs text-ink-muted">
                      {job.format || '—'} · {formatInteger(job.shots)} shots ·{' '}
                      {formatInteger(job.qubit_count)}q
                    </td>
                    <td className="max-w-64 py-2 text-xs text-err">
                      {job.error_message ? (
                        <span title={job.error_message}>{job.error_message}</span>
                      ) : (
                        <span className="text-ink-muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {jobs.hasNextPage && (
          <div className="mt-3 flex justify-center">
            <button
              type="button"
              onClick={() => jobs.fetchNextPage()}
              disabled={jobs.isFetchingNextPage}
              className="rounded-lg border border-line bg-surface-2 px-4 py-1.5 text-sm text-ink-muted hover:text-ink disabled:opacity-50"
            >
              {jobs.isFetchingNextPage ? 'Loading…' : 'Load older jobs'}
            </button>
          </div>
        )}
      </Card>
    </div>
  )
}
