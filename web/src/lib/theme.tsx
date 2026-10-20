/**
 * Theme provider — dark-first with an explicit light mode.
 *
 * The initial theme is applied before first paint by an inline script in
 * `index.html`; this provider keeps the `.light` class on `<html>` in sync
 * and persists the explicit choice. "System" follows the OS preference on
 * every change, so no reload is needed after selecting it.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

export type ThemeChoice = 'dark' | 'light' | 'system'
export type ResolvedTheme = 'dark' | 'light'

interface ThemeContextValue {
  /** What the user picked (may be `system`). */
  choice: ThemeChoice
  /** What is actually rendered right now. */
  theme: ResolvedTheme
  setChoice: (choice: ThemeChoice) => void
}

const STORAGE_KEY = 'quac.console.theme'
const ThemeContext = createContext<ThemeContextValue | null>(null)

function readStoredChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'dark' || stored === 'light' || stored === 'system') return stored
  } catch {
    // Storage unavailable — fall through to the default.
  }
  return 'dark'
}

function systemTheme(): ResolvedTheme {
  return typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-color-scheme: light)').matches
    ? 'light'
    : 'dark'
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [choice, setChoiceState] = useState<ThemeChoice>(readStoredChoice)
  const [system, setSystem] = useState<ResolvedTheme>(systemTheme)

  // Track OS preference changes so `system` stays live.
  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: light)')
    const onChange = () => setSystem(query.matches ? 'light' : 'dark')
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  const theme: ResolvedTheme = choice === 'system' ? system : choice

  useEffect(() => {
    document.documentElement.classList.toggle('light', theme === 'light')
    try {
      localStorage.setItem(STORAGE_KEY, choice)
    } catch {
      // Preference simply won't persist.
    }
  }, [theme, choice])

  const setChoice = useCallback((next: ThemeChoice) => setChoiceState(next), [])
  const value = useMemo<ThemeContextValue>(
    () => ({ choice, theme, setChoice }),
    [choice, theme, setChoice],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme must be used inside <ThemeProvider>')
  return context
}
