import { Today } from '../lib/types'

interface Chip {
  label: string
  kind: 'ok' | 'warn' | 'miss'
}

interface Verdict {
  line: string
  sub: string
  chips: Chip[]
}

function deriveVerdict(t: Today): Verdict {
  const chips: Chip[] = []
  let notes: string[] = []

  // Sleep vs goal
  const sleepMin = t.last_sleep?.duration_min
  if (sleepMin != null) {
    if (sleepMin < 7 * 60) {
      chips.push({ label: 'Short sleep', kind: 'warn' })
      const h = Math.floor(sleepMin / 60), m = Math.round(sleepMin % 60)
      notes.push(`sleep was short (${h}h ${m.toString().padStart(2, '0')}m)`)
    } else {
      chips.push({ label: 'Good sleep', kind: 'ok' })
    }
  }

  // Heart rate stale / missing
  if (t.heart_rate_is_stale === true || (t.heart_rate_days_old != null && t.heart_rate_days_old >= 1)) {
    const d = t.heart_rate_days_old ?? 1
    chips.push({ label: `HR missing ${d}d`, kind: 'miss' })
    notes.push(`heart rate hasn't synced since ${d === 1 ? 'yesterday' : `${d} days ago`}`)
  }

  // Steps vs avg (steps_today present)
  const steps = t.steps_today
  if (steps != null && steps > 0) {
    chips.push({ label: 'Steady', kind: 'ok' })
  } else if (steps != null && steps === 0) {
    chips.push({ label: 'No steps yet', kind: 'warn' })
  }

  if (chips.length === 0) {
    chips.push({ label: 'Steady', kind: 'ok' })
  }

  const line = notes.length
    ? `Steady day — good pace on activity.`
    : `Steady day — nothing needs attention.`

  const sub = notes.length
    ? `${notes[0].charAt(0).toUpperCase()}${notes[0].slice(1)}.${notes.length > 1 ? ` Also ${notes.slice(1).join(', ')}.` : ''}`
    : 'All tracked metrics are current. Open a tile for detail.'

  return { line, sub, chips }
}

export default function VerdictStrip({ data }: { data: Today }) {
  const v = deriveVerdict(data)
  return (
    <div className="verdict" role="status">
      <div className="verdict-main">
        <div className="verdict-line">{v.line}</div>
        <div className="verdict-sub">{v.sub}</div>
      </div>
      {v.chips.map((c, i) => (
        <span key={i} className={`vchip ${c.kind}`}>{c.kind === 'ok' ? '● ' : ''}{c.label}</span>
      ))}
    </div>
  )
}
