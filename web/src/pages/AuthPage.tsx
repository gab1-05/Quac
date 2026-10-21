/**
 * Sign in / create account — the console's only unauthenticated page.
 *
 * The same form serves both routes (`/login` and `/register`); mode only
 * changes the submit action and the toggle link. On success the session cookie
 * is live and the router returns to the page the visitor originally requested
 * (deep-link redirect preserved through `location.state`).
 *
 * All error codes are real API responses: 401 (bad credentials), 409
 * (username taken), 422 (validation rules) and transport failures — each is
 * rendered from the API's `{"detail": …}` envelope, never invented locally.
 */
import { useMutation } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Card, ErrorBanner, Spinner } from '../components/ui'
import { useAuth } from '../lib/auth'

type Mode = 'login' | 'register'

interface LocationState {
  from?: { pathname?: string }
}

const USERNAME_HINT: Record<Mode, string> = {
  login: 'your handle (3-32 chars, a-z 0-9 _ -)',
  register: '3-32 chars: a-z, 0-9, _ or -, starting alphanumeric',
}

const PASSWORD_HINT: Record<Mode, string> = {
  login: 'your password',
  register: 'at least 10 characters',
}

export default function AuthPage({ mode }: { mode: Mode }) {
  const { user, loading, error: bootError, login, register } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')

  const destination = (location.state as LocationState | null)?.from?.pathname ?? '/'

  const submit = useMutation({
    mutationFn: () =>
      mode === 'login' ? login({ username, password }) : register({ username, password }),
    onSuccess: () => navigate(destination, { replace: true }),
  })

  // Already signed in (or followed a redirect here after login).
  if (!loading && user) return <Navigate to={destination} replace />

  // Boot reached the API but failed hard (backend down) — show it honestly.
  if (bootError) {
    return (
      <div className="mx-auto mt-10 max-w-md">
        <ErrorBanner error={bootError} title="Cannot reach the API" />
      </div>
    )
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    submit.mutate()
  }

  const isRegister = mode === 'register'

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-2 p-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <div className="text-2xl font-bold tracking-tight text-ink-strong">Quac</div>
          <div className="text-sm text-ink-muted">
            {isRegister ? 'Create your console account' : 'Sign in to the console'}
          </div>
        </div>

        <Card>
          <form onSubmit={onSubmit} className="flex flex-col gap-4">
            <label className="flex flex-col gap-1 text-xs text-ink-muted">
              Username
              <input
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                required
                minLength={3}
                maxLength={32}
                value={username}
                onChange={(event) => {
                  setUsername(event.target.value)
                  submit.reset()
                }}
                className="rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-sm text-ink focus:border-accent focus:outline-none"
              />
              <span className="text-[11px]">{USERNAME_HINT[mode]}</span>
            </label>

            <label className="flex flex-col gap-1 text-xs text-ink-muted">
              Password
              <input
                name="password"
                type="password"
                autoComplete={isRegister ? 'new-password' : 'current-password'}
                required
                minLength={isRegister ? 10 : 1}
                maxLength={128}
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value)
                  submit.reset()
                }}
                className="rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-sm text-ink focus:border-accent focus:outline-none"
              />
              <span className="text-[11px]">{PASSWORD_HINT[mode]}</span>
            </label>

            <ErrorBanner
              error={submit.error}
              title={isRegister ? 'Registration failed' : 'Sign-in failed'}
            />

            <button
              type="submit"
              disabled={submit.isPending || username.length === 0 || password.length === 0}
              className="flex items-center justify-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-accent-ink disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submit.isPending && <Spinner />}
              {isRegister ? 'Create account' : 'Sign in'}
            </button>
          </form>

          <p className="mt-4 text-center text-xs text-ink-muted">
            {isRegister ? (
              <>
                Already registered?{' '}
                <Link to="/login" state={location.state} className="text-accent hover:underline">
                  Sign in
                </Link>
              </>
            ) : (
              <>
                No account yet?{' '}
                <Link to="/register" state={location.state} className="text-accent hover:underline">
                  Create one
                </Link>
              </>
            )}
          </p>
        </Card>

        <p className="mt-4 text-center text-[11px] text-ink-muted">
          Open registration lands on the free plan · sessions use an HTTP-only cookie
        </p>
      </div>
    </div>
  )
}
