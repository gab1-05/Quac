/**
 * Session-auth context (P1).
 *
 * Boots from `GET /v1/auth/me` using the HTTP-only session cookie the API set
 * on login/register. A 401 boot response means "anonymous" — a network
 * failure does not, and is surfaced so pages can show a real error instead of
 * pretending the user is signed out.
 *
 * The context also exposes register/login/logout mutations; each mutation
 * updates the shared query cache so every page reacts to the identity change
 * immediately (logout additionally drops all cached tenant data so a second
 * user on the same browser never sees the first user's rows).
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'
import {
  ApiError,
  getMe,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
  type AuthUserResponse,
  type LoginPayload,
  type RegisterPayload,
} from '../api/client'

export interface AuthContextValue {
  /** `null` when anonymous; `undefined` data while the boot request is in flight. */
  user: AuthUserResponse | null
  /** True until the boot `GET /v1/auth/me` settles. */
  loading: boolean
  /** Boot failure (API unreachable) — not set for a normal 401. */
  error: unknown
  /** Register a new account (server sets the session cookie). */
  register: (payload: RegisterPayload) => Promise<AuthUserResponse>
  /** Sign in with username/password (server sets the session cookie). */
  login: (payload: LoginPayload) => Promise<AuthUserResponse>
  /** End the session server-side and clear every cached tenant row. */
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

const ME_QUERY_KEY = ['auth', 'me'] as const

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  const me = useQuery<AuthUserResponse | null>({
    queryKey: ME_QUERY_KEY,
    queryFn: async () => {
      try {
        return await getMe()
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null
        throw error
      }
    },
    staleTime: 60_000,
  })

  const register = useCallback(
    async (payload: RegisterPayload) => {
      const user = await apiRegister(payload)
      queryClient.setQueryData(ME_QUERY_KEY, user)
      // Refresh usage counters (register/login return them as null).
      void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY })
      return user
    },
    [queryClient],
  )

  const login = useCallback(
    async (payload: LoginPayload) => {
      const user = await apiLogin(payload)
      queryClient.setQueryData(ME_QUERY_KEY, user)
      void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY })
      return user
    },
    [queryClient],
  )

  const logout = useCallback(async () => {
    try {
      await apiLogout()
    } catch {
      // Already signed out server-side (e.g. expired cookie) — still clear locally.
    }
    queryClient.setQueryData(ME_QUERY_KEY, null)
    // Drop every cached tenant-owned query; keep health/format probes.
    queryClient.removeQueries({
      predicate: (query) => query.queryKey[0] !== 'health' && query.queryKey[0] !== 'formats',
    })
  }, [queryClient])

  const value = useMemo<AuthContextValue>(
    () => ({
      user: me.data ?? null,
      loading: me.isLoading,
      error: me.error,
      register,
      login,
      logout,
    }),
    [me.data, me.isLoading, me.error, register, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/** Access the session; must be used under {@link AuthProvider}. */
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (context === null) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}
