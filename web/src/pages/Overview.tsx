/**
 * Overview — the console landing page.
 *
 * Everything shown here is real data from the running API or this browser:
 * liveness/version from `/healthz`, formats + platform limits from
 * `/v1/quantum/formats`, and the browser-local submission history. The
 * server-side job list and platform KPIs (queue depth, cost, success rate)
 * arrive with the identity (P1) and metrics (P10) phases — until then this
 * page deliberately shows no fabricated numbers.
 */
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { getFormats, getHealth } from '../api/client'
import { Badge, Card, ErrorBanner, Skeleton, Stat } from '../components/ui'
import { formatInteger, formatTimestamp } from '../lib/format'
import { listRecentJobs } from '../lib/history'

export default function Overview() {
  const health = useQuery({ queryKey: ['health'], queryFn: getHealth, refetchInterval: 30_000 })
  const formats = useQuery({ queryKey: ['formats'], queryFn: getFormats })
  const recent = listRecentJobs().slice(0, 5)

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

      <Card
        title="Recent submissions from this browser"
        actions={
          <Link to="/studio" className="text-xs font-medium text-accent hover:underline">
            Open Studio →
          </Link>
        }
      >
        {recent.length === 0 ? (
          <p className="text-sm text-ink-muted">
            Nothing submitted yet from this browser. Head to the{' '}
            <Link to="/studio" className="text-accent hover:underline">
              Studio
            </Link>{' '}
            to analyse and run a circuit — submissions made there appear here.
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {recent.map((job) => (
              <li key={job.job_id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
                <Link
                  to={`/jobs/${job.job_id}`}
                  className="font-mono text-xs text-accent hover:underline"
                >
                  {job.job_id.slice(0, 8)}…
                </Link>
                <Badge>{job.format}</Badge>
                <span className="text-ink-muted">{formatInteger(job.shots)} shots</span>
                <span className="ml-auto text-xs text-ink-muted">
                  {formatTimestamp(job.submitted_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-ink-muted">
          Stored in this browser only (localStorage) — the owner-scoped server-side job list ships
          with the identity phase.
        </p>
      </Card>
    </div>
  )
}
