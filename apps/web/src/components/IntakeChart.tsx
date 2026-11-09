import { useMemo, useState } from 'react'
import { fmtAmount, fmtPct } from '../lib/format'
import { AXIS_OFFSETS, bottomLabelPx, legendItemsFor, truncateLabel, type LegendSwatch as LegendSwatchKind } from './IntakeLegend'

/** Intake chart per 0.11.0 spec §4.7 — inline SVG, no new deps. */
export type IntakeView = 'week' | 'month'
export type IntakeGroup = 'vitamin' | 'mineral' | 'trace' | 'fatty_acid'

export interface IntakeDayValue {
  date: string
  food: number | null
  supp: number
  total: number | null
  pct: number | null
  completeness: number | null
  above_ul: boolean
  status: 'ok' | 'partial' | 'food_missing' | 'no_data' | string
}

export interface IntakeNutrient {
  code: string
  label: string
  unit: string
  group: IntakeGroup | string
  ref: number | null
  ref_kind?: string | null
  ul?: number | null
  ul_scope?: string | null
  days: IntakeDayValue[]
  summary?: {
    days_with_data: number
    avg_pct: number | null
    min_pct: number | null
    max_pct: number | null
    days_ge_100: number
    avg_amount?: number | null
  } | null
}

export const GROUP_LABELS: Record<string, string> = {
  vitamin: 'Vitamins',
  mineral: 'Minerals',
  trace: 'Trace elements',
  fatty_acid: 'Fatty acids',
}
export const GROUP_ORDER = ['vitamin', 'mineral', 'trace', 'fatty_acid']

const ACCENT = 'var(--color-accent, #0d7c6b)'
const WARN = 'var(--color-warn, #b26a00)'
const MUTED = 'var(--color-muted, #6b7a83)'
const BORDER = 'var(--color-border, #e2e7ea)'
const H = 300 // plot height px
const AXIS_W = 46
const TOP = 18
const BASELINE = TOP + H

function p95(vals: number[]): number {
  if (!vals.length) return 150
  const s = [...vals].sort((a, b) => a - b)
  const i = Math.min(s.length - 1, Math.floor(0.95 * s.length))
  return s[i]
}

function avg(vals: number[]): number | null {
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null
}

function fmtDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00`)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
}

interface Tip {
  x: number
  y: number
  lines: string[]
}

interface Props {
  nutrients: IntakeNutrient[]
  days: string[]
  view: IntakeView
  sortByPct: boolean
  onSelect?: (date: string, code: string) => void
}

function LegendSwatch({ kind }: { kind: LegendSwatchKind }) {
  const box: React.CSSProperties = { flex: '0 0 auto', display: 'inline-block' }
  switch (kind) {
    case 'supp':
      return <span style={{ ...box, width: 14, height: 12, borderRadius: 3, background: ACCENT }} />
    case 'food':
      return <span style={{ ...box, width: 14, height: 12, borderRadius: 3, background: ACCENT, opacity: 0.55 }} />
    case 'hatch':
      return <span style={{ ...box, width: 14, height: 12, borderRadius: 3, background: ACCENT, opacity: 0.55, backgroundImage: 'repeating-linear-gradient(45deg, rgba(255,255,255,.95) 0 2px, rgba(255,255,255,0) 2px 6px)' }} />
    case 'ref':
      return <span style={{ ...box, width: 16, height: 0, borderTop: `3px solid ${MUTED}` }} />
    case 'ul':
      return <span style={{ ...box, width: 16, height: 0, borderTop: `2px dashed ${WARN}` }} />
    case 'above-ul':
      return <span style={{ ...box, width: 14, height: 12, borderRadius: 3, background: ACCENT, opacity: 0.55, border: `2px solid ${WARN}` }} />
    case 'no-data':
      return <span style={{ ...box, width: 0, height: 12, borderLeft: `3px solid var(--color-faint, #9aa8b0)` }} />
    case 'dot':
      return <span style={{ ...box, width: 9, height: 9, borderRadius: '50%', background: ACCENT, opacity: 0.75, border: '1px solid #fff' }} />
    case 'whisker':
      return (
        <span style={{ ...box, position: 'relative', width: 12, height: 14 }}>
          <span style={{ position: 'absolute', left: 5, top: 0, bottom: 0, width: 0, borderLeft: `1.5px solid ${MUTED}` }} />
          <span style={{ position: 'absolute', left: 0, right: 0, top: 0, height: 0, borderTop: `1.5px solid ${MUTED}` }} />
          <span style={{ position: 'absolute', left: 0, right: 0, bottom: 0, height: 0, borderTop: `1.5px solid ${MUTED}` }} />
        </span>
      )
  }
}

function ChartLegend({ view }: { view: IntakeView }) {
  const items = legendItemsFor(view)
  return (
    <div className="intake-legend" data-testid="intake-legend" role="list" aria-label="Chart legend"
      style={{ display: 'flex', flexWrap: 'wrap', gap: '6px 16px', alignItems: 'center', padding: '8px 10px', marginBottom: 10, border: '1px solid var(--color-border, #e2e7ea)', borderRadius: 8, background: 'var(--color-surface, #fff)' }}>
      {items.map((i) => (
        <span key={i.key} role="listitem" data-legend-item={i.key}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, lineHeight: 1.3 }}>
          <LegendSwatch kind={i.swatch} />
          <span>{i.label}</span>
        </span>
      ))}
    </div>
  )
}

export default function IntakeChart({ nutrients, days, view, sortByPct, onSelect }: Props) {
  const [tip, setTip] = useState<Tip | null>(null)

  const prepared = useMemo(() => {
    const list = [...nutrients]
    if (sortByPct) {
      const score = (n: IntakeNutrient) => {
        const vals = n.days.map((d) => d.pct).filter((v): v is number => v != null)
        return vals.length ? avg(vals) ?? 0 : -1
      }
      list.sort((a, b) => score(b) - score(a))
    } else {
      list.sort((a, b) => GROUP_ORDER.indexOf(a.group) - GROUP_ORDER.indexOf(b.group))
    }
    const allPct = list.flatMap((n) => n.days.map((d) => d.pct)).filter((v): v is number => v != null && Number.isFinite(v))
    const rawTop = Math.max(150, Math.min(p95(allPct), 400))
    const domain = Math.ceil(rawTop / 50) * 50
    return { list, domain }
  }, [nutrients, sortByPct])

  const { list, domain } = prepared
  const y = (pct: number) => TOP + H - (Math.min(pct, domain) / domain) * H

  // layout
  const weekMode = view === 'week'
  const barW = weekMode ? 6 : 26
  const barGap = weekMode ? 2 : 0
  const clusterW = weekMode ? 7 * barW + 6 * barGap : barW
  const nutrientGap = weekMode ? 14 : 30
  const groupGap = 26
  let cursor = 8
  const layout = list.map((n, idx) => {
    const prev = list[idx - 1]
    if (idx > 0 && prev && prev.group !== n.group) cursor += groupGap
    const x = cursor
    cursor += clusterW + nutrientGap
    return { n, x }
  })
  const totalW = Math.max(cursor, 320)
  const totalH = BASELINE + bottomLabelPx(view)
  const ax = AXIS_OFFSETS[view]

  // gridlines: 25,50,75,100,150,200,...
  const grids: number[] = [25, 50, 75, 100]
  for (let v = 150; v <= domain; v += 50) grids.push(v)

  function showTip(e: React.MouseEvent | React.FocusEvent, lines: string[], cx: number, cy: number) {
    // position relative to scroll container via offset in SVG coords
    const svg = (e.currentTarget as Element).closest('svg')
    const rect = svg?.getBoundingClientRect()
    const me = e as React.MouseEvent
    const px = me.clientX != null && rect ? me.clientX - rect.left : cx
    const py = me.clientY != null && rect ? me.clientY - rect.top : cy
    setTip({ x: Math.min(Math.max(px + 12, 4), totalW - 220), y: Math.max(py - 10, 4), lines })
  }

  function dayTipLines(n: IntakeNutrient, d: IntakeDayValue): string[] {
    const ref = n.ref
    const approx = d.food != null ? '≈ ' : ''
    const totalStr = d.total != null ? `${approx}${fmtAmount(d.total, n.unit)}` : 'no data'
    const refStr = ref != null ? `of ${fmtAmount(ref, n.unit)} (${fmtPct(d.pct)})` : `(${fmtPct(d.pct)})`
    const lines = [`${n.label} · ${fmtDay(d.date)}`, `${totalStr} of ${refStr}`]
    if (d.food != null) lines.push(`Food ${fmtAmount(d.food, n.unit)}${d.completeness != null ? ` (data for ${Math.round(d.completeness * 100)}% of calories)` : ''}`)
    else if (d.status === 'food_missing') lines.push('Food: no meals logged; supplements only')
    lines.push(`Supplements ${fmtAmount(d.supp, n.unit)}`)
    if (d.completeness != null) lines.push(`Completeness ${Math.round(d.completeness * 100)}%`)
    if (n.ul != null) lines.push(`UL ${fmtAmount(n.ul, n.unit)}${n.ul_scope ? ` (${n.ul_scope})` : ''}${d.above_ul ? ' — above UL' : ''}`)
    if (d.status === 'no_data') lines.push('No data this day')
    return lines
  }

  // y-axis (sticky, separate svg)
  const axis = (
    <svg width={AXIS_W} height={totalH} aria-hidden="true" style={{ flex: '0 0 auto', position: 'sticky', left: 0, background: 'var(--color-surface, #fff)', zIndex: 2 }}>
      {grids.map((g) => (
        <text key={g} x={AXIS_W - 6} y={y(g) + 4} textAnchor="end" fontSize={10} fill={MUTED}>{g}%</text>
      ))}
      <text x={AXIS_W - 6} y={y(100) + 4} textAnchor="end" fontSize={10} fontWeight={700} fill={MUTED}>100%</text>
    </svg>
  )

  return (
    <div onMouseLeave={() => setTip(null)}>
      <ChartLegend view={view} />
    <div style={{ display: 'flex', alignItems: 'flex-start' }}>
      {axis}
      <div style={{ overflowX: 'auto', maxWidth: '100%' }}>
        <div style={{ position: 'relative', width: totalW }}>
          <svg width={totalW} height={totalH} role="img" aria-label={`Intake chart, ${view} view, percent of daily reference`}>
            <defs>
              <pattern id="intake-hatch" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
                <rect width="6" height="6" fill={ACCENT} opacity="0.55" />
                <line x1="0" y1="0" x2="0" y2="6" stroke="#fff" strokeWidth="2" />
              </pattern>
            </defs>

            {/* gridlines */}
            {grids.map((g) => (
              <line key={g} x1={0} x2={totalW} y1={y(g)} y2={y(g)}
                stroke={g === 100 ? MUTED : BORDER} strokeWidth={g === 100 ? 2 : 1} opacity={g === 100 ? 0.9 : 0.7} />
            ))}
            <text x={4} y={y(100) - 4} fontSize={10} fontWeight={700} fill={MUTED}>daily reference</text>
            {/* baseline */}
            <line x1={0} x2={totalW} y1={TOP + H} y2={TOP + H} stroke={BORDER} strokeWidth={1.5} />

            {layout.map(({ n, x }) => {
              const ulPct = n.ul != null && n.ref ? (n.ul / n.ref) * 100 : null
              const showUl = ulPct != null && ulPct <= domain && ulPct > 0
              return (
                <g key={n.code}>
                  {showUl && (
                    <line x1={x} x2={x + clusterW} y1={y(ulPct!)} y2={y(ulPct!)} stroke={WARN} strokeWidth={1.5} strokeDasharray="4 3" opacity={0.85}>
                      <title>{`UL ${fmtAmount(n.ul, n.unit)}`}</title>
                    </line>
                  )}
                  {weekMode ? (
                    n.days.map((d, i) => {
                      const bx = x + i * (barW + barGap)
                      const foodPct = d.food != null && n.ref ? (d.food / n.ref) * 100 : 0
                      const suppPct = n.ref ? (d.supp / n.ref) * 100 : 0
                      const pct = d.pct
                      const clipped = pct != null && pct > domain
                      const foodH = (Math.min(foodPct, domain) / domain) * H
                      const suppH = (Math.min(suppPct, domain - foodPct > 0 ? suppPct : domain) / domain) * H
                      const hatch = d.completeness != null && d.completeness < 0.8
                      const noData = d.status === 'no_data' || pct == null
                      const interactive = {
                        tabIndex: 0,
                        role: 'button' as const,
                        'aria-label': dayTipLines(n, d).join('. '),
                        onMouseEnter: (e: React.MouseEvent) => showTip(e, dayTipLines(n, d), bx, y(Math.min(pct ?? 0, domain))),
                        onFocus: (e: React.FocusEvent) => showTip(e, dayTipLines(n, d), bx, y(Math.min(pct ?? 0, domain))),
                        onClick: () => onSelect?.(d.date, n.code),
                        onKeyDown: (e: React.KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect?.(d.date, n.code) } },
                        style: { cursor: 'pointer', outline: 'none' },
                      }
                      if (noData) {
                        return (
                          <g key={d.date} {...interactive}>
                            <line x1={bx + barW / 2} x2={bx + barW / 2} y1={TOP + H - 4} y2={TOP + H} stroke="var(--color-faint, #9aa8b0)" strokeWidth={2} />
                          </g>
                        )
                      }
                      return (
                        <g key={d.date} {...interactive}>
                          {foodH > 0 && (
                            <rect x={bx} y={TOP + H - foodH} width={barW} height={foodH}
                              fill={hatch ? 'url(#intake-hatch)' : ACCENT} opacity={hatch ? 1 : 0.55}
                              stroke={d.above_ul ? WARN : undefined} strokeWidth={d.above_ul ? 1.5 : 0} />
                          )}
                          {suppH > 0 && (
                            <rect x={bx} y={TOP + H - foodH - suppH} width={barW} height={suppH} fill={ACCENT}
                              stroke={d.above_ul ? WARN : undefined} strokeWidth={d.above_ul ? 1.5 : 0} />
                          )}
                          {clipped && (
                            <>
                              <text x={bx + barW / 2} y={TOP + 2} textAnchor="middle" fontSize={9} fontWeight={700} fill={WARN}>▲</text>
                              <text x={bx + barW / 2} y={TOP + 12} textAnchor="middle" fontSize={8} fill={WARN}>{Math.round(pct!)}%</text>
                            </>
                          )}
                        </g>
                      )
                    })
                  ) : (
                    (() => {
                      const withData = n.days.filter((d) => d.pct != null)
                      const avgFoodPct = n.ref ? avg(withData.filter((d) => d.food != null).map((d) => (d.food! / n.ref!) * 100)) ?? 0 : 0
                      const avgSuppPct = n.ref ? avg(withData.map((d) => (d.supp / n.ref!) * 100)) ?? 0 : 0
                      const avgPct = avg(withData.map((d) => d.pct!)) ?? 0
                      const minP = withData.length ? Math.min(...withData.map((d) => d.pct!)) : 0
                      const maxP = withData.length ? Math.max(...withData.map((d) => d.pct!)) : 0
                      const clipped = avgPct > domain
                      const foodH = (Math.min(avgFoodPct, domain) / domain) * H
                      const suppH = (Math.min(avgSuppPct, domain) / domain) * H
                      const anyHatch = withData.some((d) => d.completeness != null && d.completeness < 0.8)
                      const anyUL = withData.some((d) => d.above_ul)
                      const cx = x + barW / 2
                      const dateForClick = withData[0]?.date ?? days[0] ?? ''
                      const monthLines = [
                        `${n.label} · monthly average`,
                        `≈ ${n.ref != null && avgPct != null ? fmtAmount((avgPct / 100) * n.ref, n.unit) : '—'}/day average · ${withData.length} days with data · ≥100% on ${withData.filter((d) => (d.pct ?? 0) >= 100).length} days`,
                        `Food ${n.ref != null ? fmtAmount((avgFoodPct / 100) * n.ref, n.unit) : '—'} · Supplements ${n.ref != null ? fmtAmount((avgSuppPct / 100) * n.ref, n.unit) : '—'}`,
                        ...(n.ul != null ? [`UL ${fmtAmount(n.ul, n.unit)}${n.ul_scope ? ` (${n.ul_scope})` : ''}`] : []),
                      ]
                      return (
                        <g key={n.code} tabIndex={0} role="button" aria-label={monthLines.join('. ')}
                          style={{ cursor: 'pointer', outline: 'none' }}
                          onMouseEnter={(e) => showTip(e, monthLines, cx, y(Math.min(avgPct, domain)))}
                          onFocus={(e) => showTip(e, monthLines, cx, y(Math.min(avgPct, domain)))}
                          onClick={() => dateForClick && onSelect?.(dateForClick, n.code)}
                          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); dateForClick && onSelect?.(dateForClick, n.code) } }}>
                          {withData.length === 0 ? (
                            <line x1={cx} x2={cx} y1={TOP + H - 4} y2={TOP + H} stroke="var(--color-faint, #9aa8b0)" strokeWidth={2} />
                          ) : (
                            <>
                              {foodH > 0 && <rect x={x} y={TOP + H - foodH} width={barW} height={foodH} fill={anyHatch ? 'url(#intake-hatch)' : ACCENT} opacity={anyHatch ? 1 : 0.55} stroke={anyUL ? WARN : undefined} strokeWidth={anyUL ? 1.5 : 0} />}
                              {suppH > 0 && <rect x={x} y={TOP + H - foodH - suppH} width={barW} height={suppH} fill={ACCENT} stroke={anyUL ? WARN : undefined} strokeWidth={anyUL ? 1.5 : 0} />}
                              {/* whisker */}
                              <line x1={cx} x2={cx} y1={y(Math.min(maxP, domain))} y2={y(Math.min(minP, domain))} stroke={MUTED} strokeWidth={1.5} />
                              <line x1={cx - 5} x2={cx + 5} y1={y(Math.min(maxP, domain))} y2={y(Math.min(maxP, domain))} stroke={MUTED} strokeWidth={1.5} />
                              <line x1={cx - 5} x2={cx + 5} y1={y(Math.min(minP, domain))} y2={y(Math.min(minP, domain))} stroke={MUTED} strokeWidth={1.5} />
                              {/* dots jittered deterministically by day index */}
                              {withData.map((d, i) => {
                                const jitter = ((i * 37) % 11) - 5
                                return <circle key={d.date} cx={cx + jitter} cy={y(Math.min(d.pct!, domain))} r={2.2} fill={ACCENT} opacity={0.75} stroke="#fff" strokeWidth={0.6} />
                              })}
                              {clipped && <text x={cx} y={TOP + 2} textAnchor="middle" fontSize={9} fontWeight={700} fill={WARN}>▲ {Math.round(avgPct)}%</text>}
                            </>
                          )}
                        </g>
                      )
                    })()
                  )}
                </g>
              )
            })}

            {/* nutrient labels — below the baseline: chart area ends at
                BASELINE; week labels are horizontal at BASELINE+14, month
                labels are end-anchored at BASELINE+12 and rotated -45 so
                the whole label sits below the baseline (never penetrates). */}
            {layout.map(({ n, x }) => {
              const cx = x + clusterW / 2
              return weekMode ? (
                <text key={n.code} x={cx} y={BASELINE + AXIS_OFFSETS.week.nutrient} textAnchor="middle" fontSize={9} fill={MUTED}>
                  {truncateLabel(n.label, 12)}
                </text>
              ) : (
                <text key={n.code} x={cx + 5} y={BASELINE + AXIS_OFFSETS.month.nutrientAnchor} textAnchor="end" fontSize={9} fill={MUTED}
                  transform={`rotate(-45 ${cx + 5} ${BASELINE + AXIS_OFFSETS.month.nutrientAnchor})`}>
                  {truncateLabel(n.label)}
                </text>
              )
            })}
            {/* group captions */}
            {GROUP_ORDER.map((g) => {
              const inGroup = layout.filter((l) => l.n.group === g)
              if (!inGroup.length) return null
              const x0 = inGroup[0].x
              const x1 = inGroup[inGroup.length - 1].x + clusterW
              return (
                <g key={g}>
                  <line x1={x0} x2={x1} y1={BASELINE + ax.groupLine} y2={BASELINE + ax.groupLine} stroke={BORDER} />
                  <text x={(x0 + x1) / 2} y={BASELINE + ax.groupText} textAnchor="middle" fontSize={10} fontWeight={600} fill={MUTED}>{GROUP_LABELS[g]}</text>
                </g>
              )
            })}
            {weekMode && (
              <text x={4} y={BASELINE + AXIS_OFFSETS.week.weekday} fontSize={9} fill={MUTED}>M T W T F S S</text>
            )}
          </svg>

          {tip && (
            <div style={{
              position: 'absolute', left: tip.x, top: tip.y, zIndex: 5, pointerEvents: 'none',
              background: 'var(--color-surface, #fff)', border: '1px solid var(--color-border, #e2e7ea)',
              borderRadius: 8, padding: '8px 10px', fontSize: 12, lineHeight: 1.5, maxWidth: 220,
              boxShadow: '0 4px 14px rgba(0,0,0,.12)',
            }}>
              {tip.lines.map((l, i) => <div key={i} style={{ fontWeight: i === 0 ? 700 : 400 }}>{l}</div>)}
            </div>
          )}
        </div>
      </div>
    </div>
    </div>
  )
}
