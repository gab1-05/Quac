/**
 * Overview — the console landing page.
 *
 * Everything shown here is real data from the running API for the signed-in
 * account: liveness/version from `/healthz`, formats + platform limits from
 * `/v1/quantum/formats`, plan ceilings and this month's counters from the
 * session (`GET /v1/auth/me`), and the five newest owner-scoped jobs from
 * `GET /v1/quantum/jobs`. The page deliberately shows no fabricated numbers.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  getFormats,
  getHealth,
  listJobs,
  STATUS_LABEL,
  TERMINAL_STATUSES,
  type JobStatus,
} from '../api/client'
import { Badge, Card, ErrorBanner, Skeleton, Stat } from '../components/ui'
import { formatInteger, formatTimestamp } from '../lib/format'
import { useAuth } from '../lib/auth'

const STATUS_TONE: Record<JobStatus, 'ok' | 'warn' | 'err' | 'accent' | 'neutral'> = {
  queued: 'neutral',
  processing: 'accent',
  completed: 'ok',
  failed: 'err',
  cancelled: 'warn',
}

/** Share of a monthly allowance consumed, as a 0-100 integer. */
function percent(used: number, limit: number): number {
  if (limit <= 0) return 0
  return Math.min(100, Math.round((used / limit) * 100))
}

export default function Overview() {
  const { user } = useAuth()
  const health = useQuery({ queryKey: ['health'], queryFn: getHealth, refetchInterval: 30_000 })
  const formats = useQuery({ queryKey: ['formats'], queryFn: getFormats })
  const recent = useQuery({
    queryKey: ['jobs', 'overview'],
    queryFn: () => listJobs({ limit: 5 }),
  })

  const limits = user?.limits ?? null
  const usage = user?.usage ?? null

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-bold text-ink-strong">Overview</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Analyse a circuit, run it on the selector-chosen backend, and track the job to its
          result document.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="API status">
          {health.isLoading && <Skeleton className="h-20 w-full" />}
          <ErrorBanner error={health.error} title="Cannot reach the API" />
          {health.data && (
            <div className="grid grid-cols-3 gap-2">
              <Stat label="Status" value={health.data.status} />
              <Stat label="Service" value={health.data.service} />
              <Stat label="Version" value={health.data.version} />
            </div>
          )}
          <p className="mt-3 text-xs text-ink-muted">
            Source: <code className="font-mono">GET /healthz</code>, refreshed every 30 s.
          </p>
        </Card>

        <Card title="Platform limits">
          {formats.isLoading && <Skeleton className="h-20 w-full" />}
          <ErrorBanner error={formats.error} title="Cannot load formats" />
          {formats.data && (
            <>
              <div className="grid grid-cols-2 gap-2">
                <Stat label="Max qubits" value={formatInteger(formats.data.max_qubits)} />
                <Stat label="Max shots" value={formatInteger(formats.data.max_shots)} />
              </div>
              <div className="mt-3 flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-ink-muted">Accepted formats:</span>
                {formats.data.formats.map((format) => (
                  <Badge key={format} tone="accent">
                    {format}
                  </Badge>
                ))}
              </div>
            </>
          )}
          <p className="mt-3 text-xs text-ink-muted">
            Source: <code className="font-mono">GET /v1/quantum/formats</code>.
          </p>
        </Card>
      </div>

      {user && limits && (
        <Card
          title="Account & usage"
          actions={<Badge tone={user.plan === 'free' ? 'neutral' : 'accent'}>{user.plan} plan</Badge>}
        >
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Signed in as" value={user.username} hint={user.user_id.slice(0, 8)} />
            <Stat
              label="Requests this month"
              value={usage ? `${formatInteger(usage.requests)} / ${formatInteger(limits.monthly_requests)}` : '…'}
              hint={usage ? `${percent(usage.requests, limits.monthly_requests)}% of allowance` : 'reading counters'}
            />
            <Stat
              label="Compute this month"
              value={usage ? `${(usage.compute_ms / 1000).toFixed(1)} s` : '…'}
              hint={`cap ${(limits.monthly_compute_ms / 3_600_000).toFixed(1)} h`}
            />
            <Stat
              label="Job ceilings"
              value={`${limits.max_qubits}q · ${formatInteger(limits.max_shots)} shots`}
              hint={`${limits.max_concurrent_jobs} concurrent · ${limits.requests_per_minute} req/min`}
            />
          </div>
          <p className="mt-3 text-xs text-ink-muted">
            Source: <code className="font-mono">GET /v1/auth/me</code> (your plan and this UTC
            month's counters; submissions above the ceilings are rejected at the API).
          </p>
        </Card>
      )}

      <Card
        title="Recent jobs"
        actions={
          <div className="flex items-center gap-3">
            <Link to="/jobs" className="text-xs font-medium text-accent hover:underline">
              All jobs →
            </Link>
            <Link to="/studio" className="text-xs font-medium text-accent hover:underline">
              Open Studio →
            </Link>
          </div>
        }
      >
        {recent.isLoading && <Skeleton className="h-24 w-full" />}
        <ErrorBanner error={recent.error} title="Cannot load your jobs" />
        {recent.isSuccess && recent.data.jobs.length === 0 && (
          <p className="text-sm text-ink-muted">
            No jobs yet. Head to the{' '}
            <Link to="/studio" className="text-accent hover:underline">
              Studio
            </Link>{' '}
            to analyse and run a circuit — your submissions appear here.
          </p>
        )}
        {recent.data && recent.data.jobs.length > 0 && (
          <ul className="divide-y divide-line">
            {recent.data.jobs.map((job) => (
              <li key={job.job_id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                <Badge tone={STATUS_TONE[job.status]}>{STATUS_LABEL[job.status]}</Badge>
                <Link
                  to={`/jobs/${job.job_id}`}
                  className="truncate font-mono text-xs text-accent hover:underline"
                  title={job.job_id}
                >
                  {job.job_id.slice(0, 8)}…
                </Link>
                <span className="text-xs text-ink-muted">
                  {job.format} · {formatInteger(job.shots)} shots · {formatInteger(job.qubit_count)}q
                </span>
                <span className="ml-auto text-xs text-ink-muted">
                  {formatTimestamp(job.created_at)}
                  {!TERMINAL_STATUSES.has(job.status) && ' · running'}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-ink-muted">
          Source: <code className="font-mono">GET /v1/quantum/jobs?limit=5</code> — newest first,
          scoped to <span className="font-mono">{user?.username}</span> only.
        </p>
      </Card>
    </div>
  )
}
