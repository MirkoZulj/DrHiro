/**
 * Questionnarium chart renderer (0.12.0) — hand-rolled inline SVG, no deps.
 *
 * The model returns a fenced ```chart ... ``` block inside its answer; the block
 * is JSON: {"type":"line"|"bar","title":...,"x":[...],"y":[...],
 *            "unit":...,"series":[{"name":...,"values":[...]}],
 *            "reference_lines":[{"value":n,"label":s}],"annotations":[{"index":i,"text":s}]}
 * This renderer accepts that shape (or a tolerant subset) and draws it with the
 * project's design tokens. Missing/null values render as gaps — never as zero.
 */

export interface ChartSeries {
  name?: string
  values: (number | null)[]
}

export interface ChartReferenceLine {
  value: number
  label?: string
}

export interface ChartAnnotation {
  index: number
  text: string
}

export interface ChartSpec {
  type: 'line' | 'bar'
  title?: string | null
  x?: (string | number)[]
  y?: (number | null)[]
  unit?: string | null
  series?: ChartSeries[]
  reference_lines?: ChartReferenceLine[]
  annotations?: ChartAnnotation[]
}

interface NormalisedChart {
  kind: 'line' | 'bar'
  title: string
  labels: string[]
  unit: string
  series: { name: string; values: (number | null)[] }[]
  refs: ChartReferenceLine[]
  anns: ChartAnnotation[]
  rows: number
  cols: number
}

const ACCENT = 'var(--color-accent, #0d7c6b)'
const ACCENT_STRONG = 'var(--color-accent-strong, #0a5f52)'
const MUTED = 'var(--color-muted, #6b7a83)'
const FAINT = 'var(--color-faint, #9aa8b0)'
const BORDER = 'var(--color-border, #e2e7ea)'
const WARN = 'var(--color-warn, #b26a00)'
const SURFACE = 'var(--color-surface, #fff)'

/** Tolerant ingest: accepts the documented shape and a few natural variants. */
export function parseChartSpec(raw: unknown): NormalisedChart | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, any>
  const kind = String(r.type || 'line').toLowerCase()
  if (kind !== 'line' && kind !== 'bar') return null

  // Series: prefer `series`, fall back to flat {y} / {labels + values}
  let series: { name: string; values: (number | null)[] }[] = []
  if (Array.isArray(r.series) && r.series.length) {
    series = (r.series as any[]).map((s, i) => ({
      name: String(s?.name ?? `Series ${i + 1}`),
      values: Array.isArray(s?.values)
        ? s.values.map((v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null))
        : [],
    }))
  } else if (Array.isArray(r.y)) {
    series = [{
      name: String(r.name ?? r.metric ?? 'Value'),
      values: r.y.map((v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)),
    }]
  }
  if (!series.length || series.every((s) => s.values.length === 0)) return null

  // A row count is meaningless without at least two plottable values; a block
  // whose values are all non-numeric (["a","b"]) must not render a blank chart.
  const plottable = series.reduce((n, s) => n + s.values.filter((v) => v != null).length, 0)
  if (plottable < 2) return null

  const rows = Math.max(...series.map((s) => s.values.length))
  if (rows < 2) return null

  // X labels: explicit array, else indices.
  let labels: string[] = []
  if (Array.isArray(r.x) && r.x.length >= rows) {
    labels = r.x.slice(0, rows).map((v) => String(v))
  } else if (Array.isArray(r.labels) && r.labels.length >= rows) {
    labels = r.labels.slice(0, rows).map((v) => String(v))
  } else {
    labels = Array.from({ length: rows }, (_, i) => String(i + 1))
  }

  return {
    kind,
    title: String(r.title || r.metric || ''),
    labels,
    unit: String(r.unit || ''),
    series,
    refs: Array.isArray(r.reference_lines) ? r.reference_lines.filter((x: any) => x && typeof x.value === 'number') : [],
    anns: Array.isArray(r.annotations) ? r.annotations.filter((a: any) => a && typeof a.index === 'number' && a.text) : [],
    rows,
    cols: series.length,
  }
}

/** Extract the first ```chart ... ``` fenced block from an answer string. */
export function extractChartBlock(text: string): ChartSpec | null {
  if (!text) return null
  const re = /```\s*chart\s*\n?([\s\S]*?)```/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    try {
      const spec = JSON.parse(m[1].trim())
      const parsed = parseChartSpec(spec)
      if (parsed) return spec as ChartSpec
    } catch {
      /* not valid JSON — keep scanning other blocks */
    }
  }
  return null
}

/** Strip chart fences from an answer so prose and chart are separate. */
export function stripChartBlocks(text: string): string {
  if (!text) return ''
  return text.replace(/```\s*chart\s*\n?[\s\S]*?```/gi, '').replace(/\n{3,}/g, '\n\n').trim()
}

function fmtTick(v: number, unit: string): string {
  const abs = Math.abs(v)
  let n: string
  if (abs >= 1000) n = Math.round(v).toLocaleString('en-US')
  else if (abs >= 100) n = String(Math.round(v))
  else if (abs >= 10) n = String(Math.round(v * 10) / 10)
  else n = String(Math.round(v * 100) / 100)
  return unit ? `${n}${unit}` : n
}

const W = 560
const H = 240
const PAD_L = 48
const PAD_R = 12
const PAD_T = 26
const PAD_B = 34
const PLOT_W = W - PAD_L - PAD_R
const PLOT_H = H - PAD_T - PAD_B

export default function QuestionChart({ spec }: { spec: ChartSpec }) {
  const chart = parseChartSpec(spec)
  if (!chart) return null

  // Domain over every non-null value (missing data is a gap, never zero).
  const allVals = chart.series.flatMap((s) => s.values).filter((v): v is number => v != null)
  if (!allVals.length) return null
  const refVals = chart.refs.map((r) => r.value)
  const rawMin = Math.min(...allVals, ...refVals)
  const rawMax = Math.max(...allVals, ...refVals)
  const span = rawMax - rawMin || Math.abs(rawMax) || 1
  const pad = span * 0.12
  const min = rawMin - pad
  const max = rawMax + pad

  const xOf = (i: number) => PAD_L + (chart.rows === 1 ? PLOT_W / 2 : (i / (chart.rows - 1)) * PLOT_W)
  const yOf = (v: number) => PAD_T + PLOT_H - ((v - min) / (max - min)) * PLOT_H

  // ~5 nice ticks
  const step0 = (max - min) / 4
  const mag = Math.pow(10, Math.floor(Math.log10(step0 || 1)))
  const norm = step0 / mag
  const mult = norm >= 5 ? 5 : norm >= 2 ? 2 : 1
  const step = mult * mag
  const first = Math.ceil(min / step) * step
  const ticks: number[] = []
  for (let v = first; v <= max + 1e-9; v += step) ticks.push(Number(v.toFixed(10)))
  if (!ticks.length) ticks.push(min, max)

  // Bars: grouped clusters when several series share one row.
  const clusterW = chart.kind === 'bar' ? Math.min(PLOT_W / chart.rows, 48) : 0
  const barGap = 2
  const barW = chart.cols > 1 ? (clusterW - barGap * (chart.cols - 1)) / chart.cols : clusterW

  const tickEvery = Math.max(1, Math.ceil(chart.rows / 8))

  return (
    <div style={{ margin: '10px 0', border: `1px solid ${BORDER}`, borderRadius: 10, background: SURFACE, padding: '10px 12px 12px' }}>
      {chart.title && (
        <div style={{ fontSize: 12, fontWeight: 700, color: MUTED, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>
          {chart.title}
        </div>
      )}
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={chart.title ? `Chart: ${chart.title}` : 'Chart'} style={{ display: 'block', maxWidth: '100%' }}>
        {/* gridlines + y labels */}
        {ticks.map((t) => {
          const y = yOf(t)
          return (
            <g key={`gy-${t}`}>
              <line x1={PAD_L} x2={W - PAD_R} y1={y} y2={y} stroke={BORDER} strokeWidth={1} />
              <text x={PAD_L - 6} y={y + 3} textAnchor="end" fontSize={10} fill={FAINT}>
                {fmtTick(t, chart.unit)}
              </text>
            </g>
          )
        })}

        {/* reference lines (e.g. DRI / target) */}
        {chart.refs.map((r, i) => {
          const y = yOf(r.value)
          return (
            <g key={`ref-${i}`}>
              <line x1={PAD_L} x2={W - PAD_R} y1={y} y2={y} stroke={WARN} strokeWidth={1.5} strokeDasharray="5 3" />
              {r.label && (
                <text x={W - PAD_R - 2} y={y - 4} textAnchor="end" fontSize={9} fill={WARN}>{r.label}</text>
              )}
            </g>
          )
        })}

        {/* baseline */}
        <line x1={PAD_L} x2={W - PAD_R} y1={PAD_T + PLOT_H} y2={PAD_T + PLOT_H} stroke={MUTED} strokeWidth={1.2} />

        {chart.kind === 'line' ? (
          chart.series.map((s, si) => {
            let d = ''
            let open = false
            s.values.forEach((v, i) => {
              if (v == null) { open = false; return }
              d += (open ? ' L' : ' M') + `${xOf(i).toFixed(1)},${yOf(v).toFixed(1)}`
              open = true
            })
            const stroke = si === 0 ? ACCENT : (si === 1 ? WARN : ACCENT_STRONG)
            return (
              <g key={`ln-${si}`}>
                {d && <path d={d} fill="none" stroke={stroke} strokeWidth={si === 0 ? 2.2 : 1.8} strokeLinejoin="round" />}
                {s.values.map((v, i) => v == null ? null : (
                  <circle key={`pt-${si}-${i}`} cx={xOf(i)} cy={yOf(v)} r={si === 0 ? 2.6 : 2} fill={stroke} stroke="#fff" strokeWidth={0.8} />
                ))}
              </g>
            )
          })
        ) : (
          chart.series.map((s, si) => {
            const color = si === 0 ? ACCENT : si === 1 ? WARN : ACCENT_STRONG
            return (
              <g key={`bar-${si}`}>
                {s.values.map((v, i) => {
                  if (v == null) return null
                  const cx = xOf(i)
                  const bx = chart.cols > 1 ? cx - clusterW / 2 + si * (barW + barGap) : cx - clusterW / 2
                  const y = yOf(v)
                  const h = Math.max(1, PAD_T + PLOT_H - y)
                  return <rect key={`b-${si}-${i}`} x={bx} y={y} width={Math.max(1, barW)} height={h} fill={color} opacity={si === 0 ? 0.9 : 0.7} rx={2} />
                })}
              </g>
            )
          })
        )}

        {/* annotations */}
        {chart.anns.map((a, i) => {
          const idx = Math.max(0, Math.min(chart.rows - 1, a.index))
          const cx = xOf(idx)
          return (
            <g key={`ann-${i}`}>
              <line x1={cx} x2={cx} y1={PAD_T} y2={PAD_T + PLOT_H} stroke={WARN} strokeWidth={1} strokeDasharray="3 3" />
              <text x={Math.min(Math.max(cx, PAD_L + 4), W - PAD_R - 4)} y={PAD_T + 10} textAnchor="middle" fontSize={9} fill={WARN}>
                {a.text.length > 26 ? `${a.text.slice(0, 25)}…` : a.text}
              </text>
            </g>
          )
        })}

        {/* x labels */}
        {chart.labels.map((lab, i) => {
          if (i % tickEvery !== 0 && i !== chart.rows - 1) return null
          return (
            <text key={`xl-${i}`} x={xOf(i)} y={H - 12} textAnchor="middle" fontSize={9} fill={FAINT}>
              {lab.length > 12 ? `${lab.slice(0, 11)}…` : lab}
            </text>
          )
        })}
      </svg>

      {chart.cols > 1 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 14px', marginTop: 6, fontSize: 11, color: MUTED }}>
          {chart.series.map((s, si) => (
            <span key={`lg-${si}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 10, height: 3, borderRadius: 2, background: si === 0 ? ACCENT : si === 1 ? WARN : ACCENT_STRONG }} />
              {s.name}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
