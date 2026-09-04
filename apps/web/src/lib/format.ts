/** Small formatting helpers shared across views. */

export function isoDate(d: Date = new Date()): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** ISO week number (YYYY-Www), matching the /summaries/weekly contract. */
export function isoWeek(d: Date = new Date()): string {
  const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()))
  const dayNum = date.getUTCDay() || 7
  date.setUTCDate(date.getUTCDate() + 4 - dayNum)
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1))
  const weekNo = Math.ceil(((date.getTime() - yearStart.getTime()) / 86400000 + 1) / 7)
  return `${date.getUTCFullYear()}-W${String(weekNo).padStart(2, '0')}`
}

/** Return the last N dates as ISO strings, oldest first. */
export function lastNDates(n: number, from: Date = new Date()): string[] {
  const out: string[] = []
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(from)
    d.setDate(d.getDate() - i)
    out.push(isoDate(d))
  }
  return out
}

/** Current month as YYYY-MM (local), matching /summaries/monthly. */
export function isoMonth(d: Date = new Date()): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  return `${y}-${m}`
}

/** Signed delta with a leading +/- and a kg suffix, or '—' when nullish. */
export function fmtDeltaKg(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v > 0 ? '+' : v < 0 ? '−' : '±'
  return `${sign}${Math.abs(v).toFixed(1)} kg`
}

export function fmtDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function fmtDateTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function fmtDuration(min: number): string {
  if (min == null || Number.isNaN(min)) return '—'
  const h = Math.floor(min / 60)
  const m = Math.round(min % 60)
  if (h === 0) return `${m}m`
  return `${h}h ${m}m`
}

export function fmtKcal(v: number | undefined | null): string {
  return v == null || Number.isNaN(v) ? '—' : `${Math.round(v)} kcal`
}

export function titleCase(s: string): string {
  return s
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}
