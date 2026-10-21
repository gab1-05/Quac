/**
 * Router — auth bootstraps first; the shell wraps every protected route and
 * unauthenticated visitors are redirected to `/login` (deep links preserved).
 * Unknown paths land on Overview.
 */
import type { ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import Shell from './components/Shell'
import { ErrorBanner, Spinner } from './components/ui'
import { AuthProvider, useAuth } from './lib/auth'
import AuthPage from './pages/AuthPage'
import Overview from './pages/Overview'
import Studio from './pages/Studio'
import Jobs from './pages/Jobs'
import JobDetail from './pages/JobDetail'

/**
 * Gate around every authenticated route.
 *
 * - boot in flight → spinner (honest "checking session", not a redirect loop)
 * - boot failed hard (API unreachable) → error banner
 * - anonymous → `/login`, remembering where the visitor wanted to go
 */
function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading, error } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3">
        <Spinner label="Checking session…" />
      </div>
    )
  }
  if (error) {
    return (
      <div className="mx-auto mt-10 max-w-md">
        <ErrorBanner error={error} title="Cannot reach the API" />
      </div>
    )
  }
  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }
  return <>{children}</>
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<AuthPage mode="login" />} />
          <Route path="/register" element={<AuthPage mode="register" />} />
          <Route
            element={
              <RequireAuth>
                <Shell />
              </RequireAuth>
            }
          >
            <Route index element={<Overview />} />
            <Route path="studio" element={<Studio />} />
            <Route path="jobs" element={<Jobs />} />
            <Route path="jobs/:jobId" element={<JobDetail />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
