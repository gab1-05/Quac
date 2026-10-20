/**
 * Jobs — lookup by id plus the browser-local submission history.
 *
 * There is deliberately no fabricated "all jobs" list: the owner-scoped
 * server-side listing (`GET /v1/quantum/jobs`) ships with the identity phase
 * (P1), so until then this page shows (a) any job id you paste and (b) jobs
 * submitted from this browser, stored in localStorage.
 */
import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { getJob, STATUS_LABEL, type JobStatus } from '../api/client'
import { Badge, Card, EmptyState } from '../components/ui'
import { formatTimestamp } from '../lib/format'
import { listRecentJobs, updateJobStatus, type StoredJob } from '../lib/history'

const STATUS_TONE: Record<JobStatus, 'ok' | 'warn' | 'err' | 'accent' | 'neutral'> = {
  queued: 'neutral',
  processing: 'accent',
  completed: 'ok',
  failed: 'err',
  cancelled: 'warn',
}

/** Fetch the live status of every visible history entry in one query. */
function useHistoryStatuses(jobIds: string[]): Map<string, JobStatus> {
  const query = useQuery({
    queryKey: ['job-statuses', jobIds],
    enabled: jobIds.length > 0,
    refetchInterval: 15_000,
    queryFn: async () => {
      const settled = await Promise.allSettled(jobIds.map((id) => getJob(id)))
      return settled.flatMap((entry) => (entry.status === 'fulfilled' ? [entry.value] : []))
    },
  })
  useEffect(() => {
    for (const status of query.data ?? []) updateJobStatus(status.job_id, status.status)
  }, [query.data])
  // Derive render state from the query itself (localStorage writes land after
  // render, so badges would otherwise lag one poll behind).
  return new Map((query.data ?? []).map((entry) => [entry.job_id, entry.status]))
}

export default function Jobs() {
  const navigate = useNavigate()
  const [lookup, setLookup] = useState('')
  const recent = listRecentJobs()

  // Refresh observed statuses for the visible history entries.
  const liveStatuses = useHistoryStatuses(recent.map((job) => job.job_id))

  const onSubmit = (event: React.FormEvent) => {
    event.preventDefault()
    const id = lookup.trim()
    if (id) navigate(`/jobs/${encodeURIComponent(id)}`)
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-bold text-ink-strong">Jobs</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Open any job by id, or resume one of this browser's submissions below.
        </p>
      </div>

      <Card title="Open a job">
        <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-2">
          <label className="flex flex-1 flex-col gap-1 text-xs text-ink-muted" htmlFor="job-id">
            Job id (UUIDv4)
            <input
              id="job-id"
              value={lookup}
              onChange={(event) => setLookup(event.target.value)}
              placeholder="3f6b1a9c-1f2d-4e5a-8b7c-9d0e1f2a3b4c"
              className="w-full rounded-lg border border-line bg-surface-2 px-2 py-1.5 font-mono text-sm text-ink"
            />
          </label>
          <button
            type="submit"
            disabled={lookup.trim().length === 0}
            className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-accent-ink disabled:cursor-not-allowed disabled:opacity-50"
          >
            Open
          </button>
        </form>
      </Card>

      <Card title="This browser's submissions">
        {recent.length === 0 ? (
          <EmptyState
            title="No submissions from this browser yet"
            body={
              <>
                Submit a circuit from the{' '}
                <Link to="/studio" className="text-accent hover:underline">
                  Studio
                </Link>{' '}
                and it will appear here. Cross-browser/cross-user job listing is not available yet
                — it arrives with the owner-scoped endpoint in the identity phase.
              </>
            }
          />
        ) : (
          <ul className="divide-y divide-line">
            {recent.map((job) => (
              <HistoryRow
                key={job.job_id}
                job={job}
                liveStatus={liveStatuses.get(job.job_id)}
              />
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-ink-muted">
          Stored in this browser only (localStorage, newest 25 kept). Statuses refresh every 15 s
          while this page is open.
        </p>
      </Card>
    </div>
  )
}

/** One row of the browser-local submission history. */
function HistoryRow({ job, liveStatus }: { job: StoredJob; liveStatus?: JobStatus }) {
  const status = liveStatus ?? job.last_known_status
  return (
    <li className="flex flex-wrap items-center gap-2 py-2 text-sm">
      <Link to={`/jobs/${job.job_id}`} className="font-mono text-xs text-accent hover:underline">
        {job.job_id.slice(0, 8)}…
      </Link>
      <Badge>{job.format}</Badge>
      <span className="text-ink-muted">{job.shots.toLocaleString('en-US')} shots</span>
      {status && <Badge tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Badge>}
      <span className="ml-auto text-xs text-ink-muted">{formatTimestamp(job.submitted_at)}</span>
    </li>
  )
}
