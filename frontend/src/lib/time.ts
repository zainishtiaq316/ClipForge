/** 83.4 -> "1:23.4", 3723 -> "1:02:03.0" */
export function formatTime(seconds: number, decimals = 1): string {
  const safe = Math.max(0, seconds)
  const h = Math.floor(safe / 3600)
  const m = Math.floor((safe % 3600) / 60)
  const s = safe % 60
  const sec = s.toFixed(decimals).padStart(decimals ? 3 + decimals : 2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`
}

/** Short human duration: 28.4 -> "28s", 95 -> "1m 35s" */
export function formatDuration(seconds: number): string {
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return s % 60 ? `${m}m ${s % 60}s` : `${m}m`
}

/** Parses "1:23.4", "83.4", "1:02:03" into seconds; returns null if invalid. */
export function parseTime(text: string): number | null {
  const parts = text.trim().split(':')
  if (!parts.length || parts.length > 3 || parts.some((p) => p === '' || isNaN(Number(p)))) return null
  const value = parts.reduce((acc, part) => acc * 60 + Number(part), 0)
  return value >= 0 && Number.isFinite(value) ? value : null
}

export const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

export const round3 = (value: number) => Math.round(value * 1000) / 1000
