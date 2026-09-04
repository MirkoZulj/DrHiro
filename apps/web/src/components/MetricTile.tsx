import { MetricConfig } from '../lib/types'

export type TileVariant = 'lead' | 'standard' | 'missing'

interface MetricTileProps {
  config: MetricConfig
  value: number | null | undefined
  measuredAt: string | null | undefined
  isStale: boolean | undefined
  daysOld: number | null | undefined
  source: string | null | undefined
  spark?: number[]
  delta?: { text: string; dir: 'up' | 'down' | 'flat' }
  variant?: TileVariant
  missLabel?: string
  /** Optional pre-formatted display string that overrides config.formatValue.
      Used for two-value metrics like blood pressure (systolic/diastolic). */
  displayValue?: string
  /** Optional per-category sub-rows rendered under the value (e.g. Liquid tile). */
  breakdown?: { label: string; ml: number }[]
  onClick: () => void
}

export default function MetricTile({
  config, value, measuredAt, isStale, daysOld, source, spark,
  delta, variant = value == null ? 'missing' : config.lead ? 'lead' : 'standard',
  missLabel, displayValue, breakdown, onClick,
}: MetricTileProps) {
  const display = displayValue ?? config.formatValue(value)
  const stale = isStale === true || (daysOld != null && daysOld > 1)
  const status = stale || value == null ? (value == null ? 'miss' : 'warn') : 'ok'

  if (variant === 'missing' || value == null) {
    return (
      <button type="button" className="tile missing" onClick={onClick}>
        <div className="tile-label"><span className="ticon" aria-hidden="true">{config.icon}</span>{config.label}</div>
        <div className="tile-value">— <span className="unit">{config.unit}</span></div>
        <div className="tile-meta">
          <span className="miss-badge">{missLabel || 'No data'}</span>
        </div>
        {source && <div className="tile-footer">{source}</div>}
      </button>
    )
  }

  return (
    <button
      type="button"
      className={`tile${variant === 'lead' ? ' tile-lead' : ''}`}
      onClick={onClick}
      style={{ '--metric-color': config.color } as React.CSSProperties}
    >
      <span className={`tile-status ${status}`} title={status === 'ok' ? 'On track' : 'Needs attention'} />
      <div className="tile-label"><span className="ticon" aria-hidden="true">{config.icon}</span>{config.label}</div>
      <div className="tile-value tabular">{display}<span className="unit">{config.unit}</span></div>
      {delta && (
        <div className="tile-meta">
          <span className={`delta ${delta.dir}`}>
            {delta.dir === 'up' ? '▲' : delta.dir === 'down' ? '▼' : '▸'} {delta.text}
          </span>
        </div>
      )}
      {breakdown && breakdown.length > 0 && (
        <div className="tile-breakdown">
          {breakdown.map((b) => (
            <div key={b.label} className="tile-breakdown-row">
              <span>{b.label}</span>
              <span className="tabular">{b.ml.toLocaleString()}</span>
            </div>
          ))}
        </div>
      )}
      <div className="tile-footer">
        {formatFresh(measuredAt)} {source ? `· ${source}` : ''}
      </div>
      {spark && spark.length > 1 && <Sparkline data={spark} />}
    </button>
  )
}

function Sparkline({ data }: { data: number[] }) {
  const w = 92, h = 28
  const min = Math.min(...data), max = Math.max(...data), range = (max - min) || 1
  const pts = data.map((v, i) => [
    (i / (data.length - 1)) * w,
    h - 3 - ((v - min) / range) * (h - 6),
  ])
  const d = 'M' + pts.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' L')
  const last = pts[pts.length - 1]
  return (
    <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" opacity=".85" />
      <circle cx={last[0]} cy={last[1]} r="3" fill="var(--accent)" />
    </svg>
  )
}

function formatFresh(iso?: string | null): string {
  if (!iso) return 'just now'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const diffMs = Date.now() - d.getTime()
  const min = Math.round(diffMs / 60000)
  if (min < 1) return 'just now'
  if (min < 60) return `${min}m ago`
  const h = Math.round(min / 60)
  if (h < 24) return `${h}h ago`
  const days = Math.round(h / 24)
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days}d ago`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}
