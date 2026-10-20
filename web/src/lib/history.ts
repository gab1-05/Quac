/**
 * Browser-local record of jobs submitted from this console.
 *
 * The server-side owner-scoped job list (`GET /v1/quantum/jobs`) ships with the
 * identity phase (P1); until then the Jobs page lists what *this browser*
 * submitted, stored in localStorage. No server data is fabricated — the page
 * labels the list accordingly.
 */
import type { CircuitFormat, JobStatus } from '../api/client'

export interface StoredJob {
  job_id: string
  submitted_at: string
  format: CircuitFormat
  shots: number
  /** Last lifecycle state observed while this browser was watching the job. */
  last_known_status?: JobStatus
}

const STORAGE_KEY = 'quac.console.recent-jobs'
const MAX_ENTRIES = 25

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isStoredJob(value: unknown): value is StoredJob {
  return (
    isRecord(value) &&
    typeof value.job_id === 'string' &&
    typeof value.submitted_at === 'string' &&
    typeof value.format === 'string' &&
    typeof value.shots === 'number'
  )
}

/** Recent submissions, newest first. Tolerates corrupt/absent storage. */
export function listRecentJobs(): StoredJob[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(isStoredJob).slice(0, MAX_ENTRIES)
  } catch {
    return []
  }
}

/** Record a submission (newest first, capped, de-duplicated by job id). */
export function recordJob(job: StoredJob): void {
  const next = [job, ...listRecentJobs().filter((entry) => entry.job_id !== job.job_id)].slice(
    0,
    MAX_ENTRIES,
  )
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  } catch {
    // Storage full or unavailable — the console keeps working, history just
    // won't persist across reloads.
  }
}

/** Update the observed status of a previously recorded job. */
export function updateJobStatus(jobId: string, status: JobStatus): void {
  const jobs = listRecentJobs()
  const target = jobs.find((entry) => entry.job_id === jobId)
  if (!target || target.last_known_status === status) return
  recordJob({ ...target, last_known_status: status })
}
