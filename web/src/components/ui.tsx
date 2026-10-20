/** Small shared UI primitives used across the console pages. */
import type { ReactNode } from 'react'
import { ApiError } from '../api/client'

/** Raised card with an optional header row. */
export function Card({
  title,
  actions,
  children,
  className = '',
}: {
  title?: ReactNode
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section
      className={`rounded-xl border border-line bg-surface p-5 ${className}`}
      aria-label={typeof title === 'string' ? title : undefined}
    >
      {(title || actions) && (
        <header className="mb-4 flex flex-wrap items-center justify-between gap-2">
          {title && <h2 className="text-sm font-semibold tracking-wide text-ink-strong">{title}</h2>}
          {actions}
        </header>
      )}
      {children}
    </section>
  )
}

export type BadgeTone = 'ok' | 'warn' | 'err' | 'accent' | 'neutral'

const BADGE_TONES: Record<BadgeTone, string> = {
  ok: 'border-ok/40 bg-ok/10 text-ok',
  warn: 'border-warn/40 bg-warn/10 text-warn',
  err: 'border-err/40 bg-err/10 text-err',
  accent: 'border-accent/40 bg-accent/10 text-accent',
  neutral: 'border-line-strong bg-surface-2 text-ink-muted',
}

/** Compact status/label pill. */
export function Badge({
  tone = 'neutral',
  children,
}: {
  tone?: BadgeTone
  children: ReactNode
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${BADGE_TONES[tone]}`}
    >
      {children}
    </span>
  )
}

/** Label/value metric cell. */
export function Stat({
  label,
  value,
  hint,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
}) {
  return (
    <div className="rounded-lg bg-surface-2 px-3 py-2">
      <div className="text-[11px] font-medium uppercase tracking-wide text-ink-muted">{label}</div>
      <div className="mt-0.5 font-mono text-lg text-ink-strong">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-ink-muted">{hint}</div>}
    </div>
  )
}

/** Renders an unknown thrown value as a readable, honest error banner. */
export function ErrorBanner({ error, title = 'Request failed' }: { error: unknown; title?: string }) {
  if (!error) return null
  const api = error instanceof ApiError ? error : null
  const message =
    api?.message ?? (error instanceof Error ? error.message : `unexpected error: ${String(error)}`)
  return (
    <div
      role="alert"
      className="rounded-lg border border-err/40 bg-err/10 px-4 py-3 text-sm text-err"
    >
      <div className="font-semibold">{title}</div>
      <div className="mt-1 text-ink">{message}</div>
      {api && (api.code || api.line) && (
        <div className="mt-1 font-mono text-xs text-ink-muted">
          {api.code && <span>code: {api.code}</span>}
          {api.code && api.line && ' · '}
          {api.line && <span>line: {api.line}</span>}
          {api.status > 0 && <span> · HTTP {api.status}</span>}
        </div>
      )}
    </div>
  )
}

/** Loading placeholder block. */
export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-surface-2 ${className}`} aria-hidden="true" />
}

/** Empty-state panel with an explanation. */
export function EmptyState({ title, body, action }: { title: string; body: ReactNode; action?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-line-strong px-6 py-10 text-center">
      <div className="text-sm font-semibold text-ink-strong">{title}</div>
      <div className="mx-auto mt-1 max-w-prose text-sm text-ink-muted">{body}</div>
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

/** Accessible busy indicator (text label carries meaning without motion). */
export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-ink-muted">
      <svg className="size-4 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z" />
      </svg>
      {label && <span>{label}</span>}
    </span>
  )
}

/** Collapsible block that discloses long reference text (estimate notes, JSON). */
export function Disclosure({ summary, children }: { summary: string; children: ReactNode }) {
  return (
    <details className="group mt-2">
      <summary className="cursor-pointer list-none text-xs text-ink-muted underline decoration-dotted underline-offset-2 hover:text-ink">
        {summary}
      </summary>
      <div className="mt-2 text-xs leading-relaxed text-ink-muted">{children}</div>
    </details>
  )
}
