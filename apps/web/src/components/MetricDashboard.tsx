import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authClient } from '../lib/auth'
import { MetricConfig } from '../lib/types'

type TF = 'D' | 'W' | 'M'

interface Point { date: string; value: number | null; label: string }
interface Bucketed { granularity: string; metric: string; period_label: string; period_key: string; points: Point[] }

interface NutritionPoint {
  date: string; kcal: number; protein_g: number; carbs_g: number; fat_g: number; meals_count: number
}
interface NutritionTrend { points: NutritionPoint[] }

interface LiquidPoint {
  date: string; label: string; total_ml: number | null
  water: number | null; non_alcoholic: number | null; beer: number | null
  wine: number | null; spirits: number | null; other_alcohol: number | null
}
interface LiquidsTrend {
  granularity: string; metric: string; period_label: string; period_key: string
  categories: string[]; points: LiquidPoint[]
}

const LIQUID_CATS: { key: keyof Omit<LiquidPoint, 'date'|'label'|'total_ml'>; label: string; color: string }[] = [
  { key: 'water', label: 'Water', color: 'var(--color-chart-secondary)' },
  { key: 'non_alcoholic', label: 'Non-alcoholic', color: 'var(--color-chart-tertiary)' },
  { key: 'beer', label: 'Beer', color: '#f59e0b' },
  { key: 'wine', label: 'Wine', color: '#a855f7' },
  { key: 'spirits', label: 'Spirits', color: 'var(--color-danger)' },
  { key: 'other_alcohol', label: 'Other alcohol', color: '#ec4899' },
]

interface PopupProps {
  config: MetricConfig
  onClose: () => void
  onSaved: () => void
  balanceContext?: boolean
}

const GOAL_TYPES: Record<string, string> = {
  steps: 'daily_steps', calories: 'daily_calories', water: 'daily_water',
  sleep: 'daily_sleep', activity: 'daily_activity',
}
const CALORIE_KEYS = ['calories', 'balance']

/** bounds for period navigator */
const BOUNDS: Record<TF, number> = { D: 8, W: 4, M: 0 }

export default function MetricDashboard({ config, onClose, balanceContext = false }: PopupProps) {
  const navigate = useNavigate()
  const isCalories = CALORIE_KEYS.includes(config.key)
  const isLiquid = config.key === 'water'
  const [tf, setTf] = useState<TF>('D')
  const [offset, setOffset] = useState(0)
  const [bucketed, setBucketed] = useState<Bucketed | null>(null)
  const [nutrition, setNutrition] = useState<NutritionTrend | null>(null)
  const [liquids, setLiquids] = useState<LiquidsTrend | null>(null)
  const [dailyGoal, setDailyGoal] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [source, setSource] = useState('')
  const [balanceVal, setBalanceVal] = useState<number | null>(null)
  const [todayIntake, setTodayIntake] = useState<number | null>(null)

  // Esc + scroll lock
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = prev }
  }, [onClose])

  // goal
  useEffect(() => {
    const gt = GOAL_TYPES[config.key]
    if (!gt) { setDailyGoal(null); return }
    authClient.api('/goals').then((list) => {
      const arr = Array.isArray(list) ? list : []
      const g = arr.find((x: any) => x.goal_type === gt)
      setDailyGoal(g?.target_json?.target ?? null)
    }).catch(() => {})
  }, [config.key])

  // load data on tf/offset change
  useEffect(() => {
    setLoading(true)
    const gran = tf === 'D' ? 'day' : tf === 'W' ? 'week' : 'month'
    const metric = isCalories ? 'calories' : config.trendKey
    const jobs: Promise<any>[] = []
    if (isLiquid) {
      jobs.push(authClient.api(`/trends/liquids?granularity=${gran}&offset=${offset}`).catch(() => null))
    } else {
      jobs.push(authClient.api(`/trends/bucketed?metric=${metric}&granularity=${gran}&offset=${offset}`).catch(() => null))
    }
    if (isCalories) {
      jobs.push(authClient.api('/trends?metric=calories&period=90d').catch(() => null))
      jobs.push(authClient.api('/energy-balance?days=1').catch(() => null))
    } else {
      jobs.push(authClient.api('/dashboard/today').catch(() => null))
      
    }
    Promise.all(jobs).then((r) => {
      if (isLiquid) {
        setLiquids(r[0] ?? null)
        const t = r[1]
        setSource(t ? String(config.getSource(t) ?? '') : '')
      } else {
        setBucketed(r[0])
        if (isCalories) {
          setNutrition(r[1] ?? null)
          const t = r[2]?.points || []
          if (t.length) {
            const last = t[t.length - 1]
            setTodayIntake(last.intake_kcal ?? null)
            setBalanceVal(last.balance_kcal ?? null)
          }
          setSource('Meals')
        } else {
          const t = r[1]
          setSource(t ? String(config.getSource(t) ?? '') : '')

        }
      }
      setLoading(false)
    })
  }, [tf, offset, config.key])

  if (loading) return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup metric-popup" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="modal-header"><div className="modal-title-row"><span className="modal-icon">{config.icon}</span><h2>{config.label}</h2></div><button className="modal-close" onClick={onClose}>✕</button></div>
        <div className="modal-content"><div className="loading">Loading…</div></div>
      </div>
    </div>
  )

  const points = (bucketed?.points ?? [])
  const periodLabel = bucketed?.period_label ?? ''
  const granularity = bucketed?.granularity ?? 'day'

  // goal scaled to timeframe
  const goalForTf = dailyGoal != null
    ? (granularity === 'week' ? dailyGoal * 7 : granularity === 'month' ? null : dailyGoal)
    : null
  const goalLabel = goalForTf != null ? (granularity === 'week' ? `Goal ${(dailyGoal! * 7).toLocaleString()}/wk` : `Goal ${dailyGoal!.toLocaleString()}`) : null

  const primaryAction = isCalories ? '+ Log meal' : isLiquid ? '+ Log liquid' : config.key === 'activity' ? '+ Log activity' : '+ Add reading…'
  const primaryTo = isCalories ? '/meals' : config.key === 'activity' ? '/activities' : '/activities'

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup metric-popup" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={`${config.label} details`}>
        <div className="modal-header">
          <div className="modal-title-row">
            <span className="modal-icon" aria-hidden="true">{config.icon}</span>
            <h2>{config.label}</h2>
          </div>
          <div className="modal-header-right">
            <div className="period-nav" aria-label="Period">
              <button className="pn-btn" aria-label="Previous period" disabled={offset >= BOUNDS[tf]}
                onClick={() => setOffset((o) => o + 1)}>‹</button>
              <span className="pn-label">{periodLabel}</span>
              <button className="pn-btn" aria-label="Next period" disabled={offset <= 0}
                onClick={() => setOffset((o) => Math.max(0, o - 1))}>›</button>
            </div>
            <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
          </div>
        </div>

        {/* seg directly under header */}
        <div className="popup-seg">
          <Seg tf={tf} setTf={(t) => { setTf(t); setOffset(0) }} />
          <div className="popup-sub">
            {isCalories && balanceContext
              ? (balanceVal != null ? `Energy balance ${balanceVal > 0 ? '+' : ''}${Math.round(balanceVal).toLocaleString()} kcal · ${source}` : `${source} logged`)
              : `${tf === 'D' ? 'Daily · this week' : tf === 'W' ? 'Weekly · this quarter' : 'Monthly · last 12 months'} · ${source || '—'}`}
          </div>
        </div>

        <div className="modal-content">
          {isCalories ? (
            <CalorieDashboard bucketed={bucketed} nutrition={nutrition} goal={dailyGoal}
              todayIntake={todayIntake} balanceVal={balanceVal} balanceContext={balanceContext} />
          ) : isLiquid ? (
            <LiquidDashboard liquids={liquids} />
          ) : (
            <MetricChartView points={points} config={config} goal={goalForTf} goalLabel={goalLabel} granularity={granularity}
               />
          )}
        </div>

        <div className="modal-footer">
          <button className="btn btn-primary" onClick={() => { onClose(); navigate(primaryTo) }}>{primaryAction}</button>
        </div>
      </div>
    </div>
  )
}

/* ---- Segmented control ---- */
function Seg({ tf, setTf }: { tf: TF; setTf: (t: TF) => void }) {
  return (
    <div className="seg" role="tablist" aria-label="Timeframe">
      {(['D', 'W', 'M'] as const).map((t) => (
        <button key={t} role="tab" aria-selected={tf === t} className={tf === t ? 'active' : ''} onClick={() => setTf(t)}>{t}</button>
      ))}
    </div>
  )
}

function Stat({ label, value, unit, warn }: { label: string; value: string; unit?: string; warn?: boolean }) {
  return (
    <div className="cstat">
      <div className="cs-label">{label}</div>
      <div className="cs-value tabular" style={warn ? { color: 'var(--color-warning)' } : undefined}>
        {value}{unit && <span className="unit"> {unit}</span>}
      </div>
    </div>
  )
}

/* ---- Metric chart view (non-calorie) ---- */
function MetricChartView({ points, config, goal, goalLabel, granularity}: {
  points: Point[]; config: MetricConfig; goal: number | null; goalLabel: string | null; granularity: string; 
}) {
  const present = points.filter((p) => p.value != null)
  const total = present.reduce((s, p) => s + (p.value ?? 0), 0)
  const avg = present.length ? total / present.length : null
  const peak = present.length ? present.reduce((a, b) => ((b.value ?? 0) > (a.value ?? 0) ? b : a)) : null
  const tfLabel = granularity === 'day' ? 'DAY' : granularity === 'week' ? 'WEEK' : 'MONTH'

  const fmtVal = (v: number) => {
    if (config.key === 'sleep') return `${Math.floor(v / 60)}h ${Math.round(v % 60)}m`
    if (config.key === 'steps' || v >= 10000) return v.toLocaleString()
    return `${Math.round(v).toLocaleString()}`
  }
  const totalDisplay = config.key === 'steps' ? total.toLocaleString()
    : config.key === 'sleep' ? fmtVal(total)
    : present.length ? fmtVal(present[present.length - 1].value!)
    : '—'

  return (
    <>
      <div className="chart-card">
        <div className="chart-stats">
          <Stat label="Total" value={totalDisplay} unit={config.key === 'steps' ? config.unit : config.key === 'sleep' ? '' : config.unit} />
          <Stat label={`AVG/${tfLabel}`} value={avg != null ? fmtVal(avg) : '—'} />
          <Stat label="Peak" value={peak ? `${fmtVal(peak.value!)}${peak.label ? ` · ${peak.label}` : ''}` : '—'} />
        </div>
        <LineChart series={[{ data: points.map((p) => p.value), color: config.color, label: config.label }]}
          labels={points.map((p) => p.label)}
          goal={goal}
          goalLabel={goalLabel}
          avg={avg}
          fmtY={(v) => (v >= 10000 ? `${(v / 1000).toFixed(0)}k` : config.key === 'sleep' ? `${Math.round(v / 60)}h` : String(Math.round(v)))}
          unit={config.unit}
        />
      </div>
    </>
  )
}

/* ---- Calories view (two charts) ---- */
function CalorieDashboard({ bucketed, nutrition, goal, todayIntake, balanceVal, balanceContext }: {
  bucketed: Bucketed | null; nutrition: NutritionTrend | null; goal: number | null
  todayIntake: number | null; balanceVal: number | null; balanceContext?: boolean
}) {
  const points = bucketed?.points ?? []
  const granularity = bucketed?.granularity ?? 'day'
  const tfLabel = granularity === 'day' ? 'DAY' : granularity === 'week' ? 'WEEK' : 'MONTH'
  const present = points.filter((p) => p.value != null)
  const total = present.reduce((s, p) => s + (p.value ?? 0), 0)
  const avg = present.length ? total / present.length : null
  const goalLine = granularity === 'month' ? null : (goal ?? 2200)

  const macroPoints = nutrition?.points ?? []
  const pS = macroPoints.map((p) => { const t = p.protein_g + p.carbs_g + p.fat_g; return t > 0 ? +(p.protein_g / t * 100).toFixed(1) : null })
  const cS = macroPoints.map((p) => { const t = p.protein_g + p.carbs_g + p.fat_g; return t > 0 ? +(p.carbs_g / t * 100).toFixed(1) : null })
  const fS = macroPoints.map((p) => { const t = p.protein_g + p.carbs_g + p.fat_g; return t > 0 ? +(p.fat_g / t * 100).toFixed(1) : null })
  const macroLabels = macroPoints.map((p) => p.date.slice(5))
  const today = macroPoints.length ? macroPoints[macroPoints.length - 1] : undefined
  const todayMacros = today && (today.protein_g + today.carbs_g + today.fat_g) > 0
    ? [
        { n: 'Protein', pct: +(today.protein_g / (today.protein_g + today.carbs_g + today.fat_g) * 100).toFixed(0), g: Math.round(today.protein_g), c: 'var(--color-macro-protein)' },
        { n: 'Carbs', pct: +(today.carbs_g / (today.protein_g + today.carbs_g + today.fat_g) * 100).toFixed(0), g: Math.round(today.carbs_g), c: 'var(--color-macro-carbs)' },
        { n: 'Fat', pct: +(today.fat_g / (today.protein_g + today.carbs_g + today.fat_g) * 100).toFixed(0), g: Math.round(today.fat_g), c: 'var(--color-macro-fat)' },
      ]
    : []

  const heroVal = balanceContext
    ? (balanceVal != null ? `${balanceVal > 0 ? '+' : ''}${Math.round(balanceVal).toLocaleString()}` : '—')
    : (todayIntake != null ? Math.round(todayIntake).toLocaleString() : present.length ? Math.round(total).toLocaleString() : '—')

  return (
    <>
      <div className="chart-card">
        <div className="chart-stats">
          <Stat label={balanceContext ? 'Balance' : 'Intake'} value={heroVal} unit="kcal" />
          <Stat label={`AVG/${tfLabel}`} value={avg != null ? Math.round(avg).toLocaleString() : '—'} unit="kcal" />
          <Stat label="vs goal" value={goalLine != null && (balanceContext ? balanceVal != null : true) && todayIntake != null
            ? `${todayIntake - (goal ?? 2200) > 0 ? '+' : ''}${Math.round(todayIntake - (goal ?? 2200)).toLocaleString()}`
            : '—'} warn={goalLine != null && todayIntake != null && todayIntake > (goal ?? 2200)} />
        </div>
        <LineChart series={[{ data: points.map((p) => p.value), color: 'var(--color-chart-secondary)', label: 'Intake' }]}
          labels={points.map((p) => p.label)} goal={goalLine} goalLabel={goalLine ? `Goal ${Math.round(goalLine).toLocaleString()}` : null}
          avg={avg} fmtY={(v) => v >= 10000 ? `${(v / 1000).toFixed(0)}k` : String(Math.round(v))} unit="kcal" />
      </div>

      <div className="chart-card">
        <div className="cs-label macro-title">Macro distribution · % of daily intake</div>
        <div className="legend">
          <span className="li"><span className="sw" style={{ background: 'var(--color-macro-protein)' }} />Protein</span>
          <span className="li"><span className="sw" style={{ background: 'var(--color-macro-carbs)' }} />Carbs</span>
          <span className="li"><span className="sw" style={{ background: 'var(--color-macro-fat)' }} />Fat</span>
        </div>
        <LineChart series={[
          { data: pS, color: 'var(--color-macro-protein)', label: 'Protein' },
          { data: cS, color: 'var(--color-macro-carbs)', label: 'Carbs' },
          { data: fS, color: 'var(--color-macro-fat)', label: 'Fat' },
        ]} labels={macroLabels} fmtY={(v) => `${Math.round(v)}%`} />
        {todayMacros.length > 0 && (
          <div className="macro-bars">
            {todayMacros.map((m) => (
              <div key={m.n} className="mbar-row">
                <span className="mbar-label">{m.n}</span>
                <span className="mbar-track"><span className="mbar-fill" style={{ width: `${m.pct}%`, background: m.c }} /></span>
                <span className="mbar-val tabular">{m.pct}% · {m.g} g</span>
              </div>
            ))}
          </div>
        )}
        {macroPoints.length === 0 && <div className="empty">No meal data yet for macro distribution.</div>}
      </div>
    </>
  )
}

/* ---- Liquid view (total chart + category distribution) ---- */
function LiquidDashboard({ liquids }: { liquids: LiquidsTrend | null }) {
  const points = liquids?.points ?? []
  const granularity = liquids?.granularity ?? 'day'
  const tfLabel = granularity === 'day' ? 'DAY' : granularity === 'week' ? 'WEEK' : 'MONTH'
  const present = points.filter((p) => p.total_ml != null)
  const total = present.reduce((s, p) => s + (p.total_ml ?? 0), 0)
  const avg = present.length ? total / present.length : null

  const labels = points.map((p) => p.label)
  // today's per-category breakdown (last point)
  const last = points[points.length - 1]
  const todayCats = last ? LIQUID_CATS
    .map((c) => ({ label: c.label, ml: (last[c.key] as number | null) ?? 0, color: c.color }))
    .filter((c) => c.ml > 0)
    : []
  const todayTotal = todayCats.reduce((s, c) => s + c.ml, 0)

  return (
    <>
      <div className="chart-card">
        <div className="chart-stats">
          <Stat label="Total" value={present.length ? Math.round(total).toLocaleString() : '—'} unit="ml" />
          <Stat label={`AVG/${tfLabel}`} value={avg != null ? Math.round(avg).toLocaleString() : '—'} unit="ml" />
          <Stat label="Today" value={todayTotal > 0 ? Math.round(todayTotal).toLocaleString() : '—'} unit="ml" />
        </div>
        <LineChart series={[{ data: points.map((p) => p.total_ml), color: 'var(--color-chart-secondary)', label: 'Liquid' }]}
          labels={labels} avg={avg}
          fmtY={(v) => v >= 10000 ? `${(v / 1000).toFixed(0)}k` : String(Math.round(v))} unit="ml" />
      </div>

      <div className="chart-card">
        <div className="cs-label macro-title">Drink category distribution · ml</div>
        <div className="legend">
          {LIQUID_CATS.map((c) => (
            <span key={c.key} className="li"><span className="sw" style={{ background: c.color }} />{c.label}</span>
          ))}
        </div>
        <LineChart series={LIQUID_CATS.map((c) => ({
          data: points.map((p) => p[c.key] as number | null),
          color: c.color,
          label: c.label,
        }))} labels={labels} fmtY={(v) => v >= 10000 ? `${(v / 1000).toFixed(0)}k` : String(Math.round(v))} unit="ml" />
        {todayCats.length > 0 ? (
          <div className="macro-bars">
            {todayCats.map((c) => (
              <div key={c.label} className="mbar-row">
                <span className="mbar-label">{c.label}</span>
                <span className="mbar-track"><span className="mbar-fill" style={{ width: `${(c.ml / todayTotal) * 100}%`, background: c.color }} /></span>
                <span className="mbar-val tabular">{Math.round(c.ml / todayTotal * 100)}% · {Math.round(c.ml)} ml</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="empty">No liquid data yet for this period.</div>
        )}
      </div>
    </>
  )
}

/* ---- SVG line chart: dots, average line, goal line, thinned labels ---- */
function LineChart({ series, labels, goal, goalLabel, avg, fmtY, unit }: {
  series: { data: (number | null)[]; color: string; label?: string }[]
  labels: string[]
  goal?: number | null
  goalLabel?: string | null
  avg?: number | null
  fmtY?: (v: number) => string
  unit?: string
}) {
  const W = 720, H = 260, P = { l: 44, r: 14, t: 26, b: 30 }
  const iw = W - P.l - P.r, ih = H - P.t - P.b
  const all = series.flatMap((s) => s.data.filter((v): v is number => v != null))
  const dataMax = Math.max(...all, 1)
  // Snap the y-axis to a "nice" step (1, 2, 2.5, 5, 10 × 10^n) so gridlines
  // sit on round numbers (1000, 2000, 2500, …) instead of raw maxima.
  const rough = dataMax / 4
  const mult = Math.pow(10, Math.floor(Math.log10(Math.max(rough, 1e-9))))
  const norm = rough / mult  // 1..10
  let step: number
  if (norm < 1.5) step = 1
  else if (norm < 3) step = 2
  else if (norm < 4) step = 2.5
  else if (norm < 7) step = 5
  else step = 10
  step *= mult
  const max = Math.ceil(dataMax / step) * step
  const n = Math.max(...series.map((s) => s.data.length), 1)
  const X = (i: number) => P.l + (n === 1 ? iw / 2 : (i / (n - 1)) * iw)
  const Y = (v: number) => P.t + ih - (v / max) * ih
  const tc = 'var(--color-text-muted)', bc = 'var(--color-border)', sc = 'var(--color-surface-subtle)'
  const surface = 'var(--color-surface)'

  // major gridlines every `step` (plus the zero baseline)
  const ticks: number[] = []
  for (let v = 0; v <= max; v += step) ticks.push(v)

  // thin x labels: max ~64px/label, always show first + last
  const showLabel = (i: number) => {
    const step = Math.max(1, Math.floor(64 / (iw / Math.max(n - 1, 1))))
    return i === 0 || i === n - 1 || i % step === 0
  }

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img" aria-label="Metric chart">
      {ticks.map((yv, gi) => {
        const y = Y(yv)
        return (
          <g key={gi}>
            <line x1={P.l} x2={W - P.r} y1={y} y2={y} stroke={yv === 0 ? bc : sc} strokeWidth="1" />
            <text x={P.l - 8} y={y + 4} textAnchor="end" fontSize="10" fill={tc}>
              {fmtY ? fmtY(yv) : yv.toLocaleString()}
            </text>
          </g>
        )
      })}
      {labels.map((lab, i) => showLabel(i) && (
        <text key={i} x={X(i)} y={H - 8} textAnchor={i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle'} fontSize="10" fill={tc}>{lab}</text>
      ))}
      {/* goal line */}
      {goal != null && (
        <g>
          <line x1={P.l} x2={W - P.r} y1={Y(goal)} y2={Y(goal)} stroke="var(--color-goal)" strokeWidth="1.2" strokeDasharray="5 5" opacity=".7" />
          {goalLabel && <text x={W - P.r} y={Y(goal) - 5} textAnchor="end" fontSize="10" fontWeight="600" fill="var(--color-text-secondary)">{goalLabel}</text>}
        </g>
      )}
      {/* dotted average line */}
      {avg != null && (
        <g>
          <line x1={P.l} x2={W - P.r} y1={Y(avg)} y2={Y(avg)} stroke="var(--color-text-secondary)" strokeWidth="1.4" strokeDasharray="2 4" opacity=".6" />
          <text x={P.l + 4} y={Y(avg) - 5} fontSize="10" fontWeight="600" fill="var(--color-text-secondary)">avg {fmtY ? fmtY(avg) : Math.round(avg).toLocaleString()}{unit ? ` ${unit}` : ''}</text>
        </g>
      )}
      {series.map((s, si) => {
        const col = s.color
        let d = '', started = false
        s.data.forEach((v, i) => {
          if (v == null) { started = false; return }
          d += (started ? ' L' : ' M') + `${X(i).toFixed(1)},${Y(v).toFixed(1)}`
          started = true
        })
        return (
          <g key={si}>
            {d && <path d={d} fill="none" stroke={col} strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />}
            {s.data.map((v, i) => {
              if (v != null) return null
              let a = i - 1; while (a >= 0 && s.data[a] == null) a--
              let b = i + 1; while (b < n && s.data[b] == null) b++
              if (a >= 0 && b < n && s.data[a] != null && s.data[b] != null) {
                return (
                  <line key={`gap-${i}`} x1={X(a)} y1={Y(s.data[a]!)} x2={X(b)} y2={Y(s.data[b]!)}
                    stroke="var(--color-missing)" strokeWidth="1.5" strokeDasharray="3 4" opacity=".8" />
                )
              }
              return null
            })}
            {s.data.map((v, i) => {
              if (v == null) {
                return <circle key={`m-${i}`} cx={X(i)} cy={Y(0)} r="3" fill="none" stroke="var(--color-missing)" strokeWidth="1.4" strokeDasharray="2 2" />
              }
              return <circle key={i} cx={X(i)} cy={Y(v)} r="3.6" fill={surface} stroke={col} strokeWidth="2.2" />
            })}
          </g>
        )
      })}
    </svg>
  )
}
