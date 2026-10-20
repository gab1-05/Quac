/**
 * Application shell — brand header, left navigation, API status pill, theme
 * control, and the routed content outlet.
 */
import { useQuery } from '@tanstack/react-query'
import { NavLink, Outlet } from 'react-router-dom'
import { getHealth } from '../api/client'
import { useTheme } from '../lib/theme'
import { Badge } from './ui'

const NAV_ITEMS = [
  { to: '/', label: 'Overview', end: true },
  { to: '/studio', label: 'Studio' },
  { to: '/jobs', label: 'Jobs' },
]

function navClassName(isActive: boolean, horizontal = false): string {
  const base = horizontal
    ? 'px-3 py-1.5 rounded-lg text-sm'
    : 'block px-3 py-2 rounded-lg text-sm border-l-2'
  const state = isActive
    ? 'border-accent bg-accent/10 text-accent font-medium'
    : 'border-transparent text-ink-muted hover:bg-surface-2 hover:text-ink'
  return `${base} ${state}`
}

function navLink(isActive: boolean, horizontal = false): string {
  return navClassName(isActive, horizontal)
}

/** Live API status pill (polls /healthz every 30 s). */
function StatusPill() {
  const health = useQuery({
    queryKey: ['health'],
    queryFn: getHealth,
    refetchInterval: 30_000,
    retry: 1,
  })

  let tone: 'ok' | 'err' | 'neutral' = 'neutral'
  let label = 'API: checking…'
  if (health.isError) {
    tone = 'err'
    label = 'API unreachable'
  } else if (health.data) {
    tone = 'ok'
    label = `API ok · v${health.data.version}`
  }

  return (
    <span aria-live="polite" title="GET /healthz">
      <Badge tone={tone}>{label}</Badge>
    </span>
  )
}

/** Cycles the theme choice: dark → light → system. */
function ThemeToggle() {
  const { choice, setChoice } = useTheme()
  const next = choice === 'dark' ? 'light' : choice === 'light' ? 'system' : 'dark'
  const label = choice === 'system' ? 'System' : choice === 'dark' ? 'Dark' : 'Light'
  return (
    <button
      type="button"
      onClick={() => setChoice(next)}
      className="rounded-lg border border-line bg-surface-2 px-2.5 py-1 text-xs text-ink-muted hover:border-line-strong hover:text-ink"
      aria-label={`Theme: ${label}. Switch to ${next}.`}
    >
      Theme: {label}
    </button>
  )
}

export default function Shell() {
  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-10 focus:rounded-lg focus:bg-accent focus:px-3 focus:py-2 focus:text-accent-ink"
      >
        Skip to content
      </a>

      {/* Sidebar (desktop) */}
      <aside className="hidden w-52 shrink-0 flex-col border-r border-line bg-surface md:flex">
        <div className="border-b border-line px-4 py-4">
          <div className="text-lg font-bold tracking-tight text-ink-strong">Quac</div>
          <div className="text-xs text-ink-muted">quantum simulation console</div>
        </div>
        <nav className="flex flex-1 flex-col gap-1 p-3" aria-label="Primary">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => navLink(isActive)}>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-line px-3 py-3 text-[11px] text-ink-muted">
          Local console · auth arrives with P1
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-3">
          <div className="md:hidden">
            <span className="text-lg font-bold tracking-tight text-ink-strong">Quac</span>
          </div>
          {/* Mobile nav row */}
          <nav className="flex gap-1 md:hidden" aria-label="Primary">
            {NAV_ITEMS.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => navLink(isActive, true)}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <StatusPill />
            <ThemeToggle />
          </div>
        </header>

        <main id="main" className="mx-auto w-full max-w-5xl flex-1 p-4 md:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
