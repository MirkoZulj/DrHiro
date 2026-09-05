import { MetricConfig } from '../lib/types'

interface MetricCardProps {
  config: MetricConfig
  value: number | null | undefined
  measuredAt: string | null | undefined
  isStale: boolean | undefined
  daysOld: number | null | undefined
  source: string | null | undefined
  onClick: () => void
}

export default function MetricCard({ config, value, measuredAt, isStale, daysOld, source, onClick }: MetricCardProps) {
  const displayValue = config.formatValue(value)
  const stale = isStale === true || (daysOld != null && daysOld > 1)

  return (
    <button
      type="button"
      className={`metric-card${stale ? ' stale' : ''}`}
      onClick={onClick}
      style={{ '--metric-color': config.color } as React.CSSProperties}
    >
      <div className="metric-card-icon" aria-hidden="true">
        <span className="metric-icon">{config.icon}</span>
      </div>
      <div className="metric-card-body">
        <div className="metric-card-value tabular">{displayValue}</div>
        <div className="metric-card-label">{config.label}</div>
        {measuredAt && (
          <div className="metric-card-meta">
            {stale && <span className="stale-badge">stale</span>}
            <span className="measured-at">{formatMeta(measuredAt, source)}</span>
          </div>
        )}
        {!measuredAt && value == null && (
          <div className="metric-card-meta">
            <span className="missing-badge">No data</span>
          </div>
        )}
      </div>
    </button>
  )
}

function formatMeta(iso: string, source?: string | null): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMin = Math.round(diffMs / 60000)
  if (diffMin < 1) return source ? `just now · ${source}` : 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffH = Math.round(diffMin / 60)
  if (diffH < 24) return `${diffH}h ago`
  const diffD = Math.round(diffH / 24)
  if (diffD === 1) return 'yesterday'
  if (diffD < 7) return `${diffD}d ago`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}