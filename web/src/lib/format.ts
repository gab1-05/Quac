/** Display formatting helpers shared by the console pages. */

/** Human-readable byte count (B → TiB). */
export function formatBytes(bytes: number): string {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  const rendered =
    value >= 100 || Number.isInteger(value) ? Math.round(value).toString() : value.toFixed(1)
  return `${rendered} ${units[unit]}`
}

/** Human-readable duration with sensible precision. */
export function formatSeconds(value: number): string {
  if (value <= 0) return '0 s'
  if (value < 0.01) return `${(value * 1000).toFixed(0)} ms`
  if (value < 1) return `${value.toFixed(2)} s`
  if (value < 60) return `${value.toFixed(1)} s`
  return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`
}

/** `[low, high]` duration band, e.g. "0.06 s – 0.46 s". */
export function formatSecondsBand(band: { low: number; high: number }): string {
  return `${formatSeconds(band.low)} – ${formatSeconds(band.high)}`
}

/** `[low, high]` USD band with sub-cent precision when needed. */
export function formatUsdBand(band: { low: number; high: number }): string {
  const fmt = (value: number): string =>
    value === 0 ? '$0.00' : value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`
  return `${fmt(band.low)} – ${fmt(band.high)}`
}

/** Thousands-separated integer. */
export function formatInteger(value: number): string {
  return value.toLocaleString('en-US')
}

/** Locale timestamp from an RFC 3339 string (falls back to the raw string). */
export function formatTimestamp(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

/** Duration between two RFC 3339 timestamps, e.g. "842 ms" / "3.1 s". */
export function formatDuration(fromIso: string, toIso: string): string {
  const from = new Date(fromIso).getTime()
  const to = new Date(toIso).getTime()
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return '—'
  return formatSeconds((to - from) / 1000)
}
