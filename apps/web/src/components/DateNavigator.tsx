import { isoDate } from '../lib/format'

interface DateNavigatorProps {
  date: Date
  onChange: (d: Date) => void
}

export default function DateNavigator({ date, onChange }: DateNavigatorProps) {
  function shift(days: number) {
    const next = new Date(date)
    next.setDate(next.getDate() + days)
    onChange(next)
  }

  const label = date.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  })

  const isToday = isoDate(date) === isoDate()

  return (
    <div className="date-navigator">
      <button
        className="date-nav-btn"
        onClick={() => shift(-1)}
        aria-label="Previous day"
      >
        ←
      </button>
      <span className="date-nav-label">{isToday ? 'Today' : label}</span>
      <button
        className="date-nav-btn"
        onClick={() => shift(1)}
        aria-label="Next day"
      >
        →
      </button>
    </div>
  )
}