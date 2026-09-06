import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'
import { Today, MetricConfig } from '../lib/types'
import MetricTile from '../components/MetricTile'
import MetricDashboard from '../components/MetricDashboard'
import VerdictStrip from '../components/VerdictStrip'

const METRICS: MetricConfig[] = [
  { key: 'steps', label: 'Steps', unit: 'steps', icon: '👟', color: 'var(--color-accent)', trendKey: 'steps', lead: true,
    formatValue: (v) => (v != null ? v.toLocaleString() : '—'), getValue: (t) => t.steps_today,
    getMeasuredAt: (t) => t.steps_measured_at, getIsStale: (t) => t.steps_is_stale, getDaysOld: (t) => t.steps_days_old, getSource: (t) => t.steps_source_provider },
  { key: 'sleep', label: 'Sleep', unit: 'hours', icon: '🌙', color: 'var(--color-chart-tertiary)', trendKey: 'sleep_duration_min', lead: true,
    formatValue: (v) => { if (v == null) return '—'; const h = Math.floor(v / 60), m = Math.round(v % 60); return h === 0 ? `${m}m` : `${h}h ${m}m` },
    getValue: (t) => t.last_sleep?.duration_min, getMeasuredAt: (t) => t.last_sleep_measured_at, getIsStale: (t) => t.last_sleep_is_stale, getDaysOld: (t) => t.last_sleep_days_old, getSource: (t) => t.last_sleep_source_provider },
  { key: 'weight', label: 'Weight', unit: 'kg', icon: '⚖️', color: 'var(--color-chart-tertiary)', trendKey: 'weight_kg', lead: true,
    formatValue: (v) => (v != null && typeof v === 'number' ? `${v.toFixed(1)}` : '—'),
    getValue: (t) => { const w = t.latest_weight_kg as unknown; if (w == null) return null; if (typeof w === 'number') return w; if (typeof w === 'object' && 'value' in (w as Record<string, unknown>)) return ((w as Record<string, unknown>).value as number) ?? null; return null },
    getMeasuredAt: (t) => t.latest_weight_measured_at, getIsStale: (t) => t.latest_weight_is_stale, getDaysOld: (t) => t.latest_weight_days_old, getSource: (t) => t.latest_weight_source_provider },
  { key: 'activity', label: 'Activity', unit: 'kcal', icon: '🏃', color: 'var(--color-success)', trendKey: 'activity_kcal', lead: true,
    formatValue: (v) => (v != null ? `${Math.round(v)}` : '—'), getValue: () => null, getMeasuredAt: () => new Date().toISOString(), getIsStale: () => undefined, getDaysOld: () => null, getSource: () => 'computed' },
  { key: 'blood_pressure', label: 'Blood pressure', unit: 'mmHg', icon: '🩺', color: 'var(--color-danger)', trendKey: 'blood_pressure_systolic',
    formatValue: (v) => (v != null ? `${Math.round(v)}` : '—'), getValue: (t) => t.latest_bp?.systolic_mmhg,
    getMeasuredAt: (t) => t.latest_bp_measured_at, getIsStale: (t) => t.latest_bp_is_stale, getDaysOld: (t) => t.latest_bp_days_old, getSource: (t) => t.latest_bp_source_provider },
  { key: 'water', label: 'Liquid', unit: 'ml', icon: '💧', color: 'var(--color-chart-secondary)', trendKey: 'water_ml',
    formatValue: (v) => (v != null ? `${Math.round(v)}` : '—'), getValue: (t) => t.liquids_today?.total_ml ?? t.water_ml_today ?? 0,
    getMeasuredAt: (t) => t.water_measured_at, getIsStale: (t) => t.water_is_stale, getDaysOld: (t) => t.water_days_old, getSource: (t) => t.water_source_provider },
  { key: 'calories', label: 'Calories', unit: 'kcal', icon: '🍽️', color: 'var(--color-warning)', trendKey: 'calories_kcal',
    formatValue: (v) => (v != null ? `${Math.round(v)}` : '—'), getValue: (t) => t.calories_kcal_today,
    getMeasuredAt: (t) => t.calories_measured_at, getIsStale: (t) => t.calories_is_stale, getDaysOld: (t) => t.calories_days_old, getSource: (t) => t.calories_source_provider },
  { key: 'heart_rate', label: 'Heart rate', unit: 'bpm', icon: '❤️', color: 'var(--color-danger)', trendKey: 'heart_rate_bpm',
    formatValue: (v) => (v != null ? `${Math.round(v)}` : '—'), getValue: (t) => t.heart_rate_bpm,
    getMeasuredAt: (t) => t.heart_rate_measured_at, getIsStale: (t) => t.heart_rate_is_stale, getDaysOld: (t) => t.heart_rate_days_old, getSource: (t) => t.heart_rate_source_provider },
  { key: 'balance', label: 'Calorie balance', unit: 'kcal', icon: '⚖️', color: 'var(--color-chart-secondary)', trendKey: 'balance_kcal',
    formatValue: (v) => (v != null ? `${v > 0 ? '+' : ''}${Math.round(v)}` : '—'), getValue: () => null, getMeasuredAt: () => new Date().toISOString(), getIsStale: () => undefined, getDaysOld: () => null, getSource: () => 'computed' },
]

const HERO = ['steps', 'sleep', 'weight', 'activity']
const VITALS = ['blood_pressure', 'water', 'calories', 'heart_rate', 'balance']

export default function Home() {
  const [data, setData] = useState<Today | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<MetricConfig | null>(null)
  const [balanceContext, setBalanceContext] = useState(false)
  const [activityBurned, setActivityBurned] = useState<number | null>(null)
  const [balanceKcal, setBalanceKcal] = useState<number | null>(null)
  const [goals, setGoals] = useState<Record<string, number>>({})

  useEffect(() => {
    authClient.api('/goals').then((list) => {
      const arr = Array.isArray(list) ? list : []
      const m: Record<string, number> = {}
      for (const g of arr) {
        if (g.target_json?.target) m[g.goal_type] = g.target_json.target
      }
      setGoals(m)
    }).catch(() => {})
  }, [])

  const refreshSpecial = () => {
    authClient.api('/energy-balance?days=1').then((r) => {
      const pts = r.points ?? []
      if (pts.length) {
        const last = pts[pts.length - 1]
        setActivityBurned(last.burned_kcal ?? 0)
        setBalanceKcal(last.balance_kcal ?? 0)
      }
    }).catch(() => {})
  }
  const reload = () => { refreshSpecial(); authClient.api('/dashboard/today').then(setData).catch(() => {}) }

  useEffect(() => { refreshSpecial() }, [])
  useEffect(() => { authClient.api('/dashboard/today').then(setData).catch((e) => setError(String(e))) }, [])

  if (error) return <div className="error">{error}</div>
  if (!data) return <div className="loading">Loading…</div>

  const tileValue = (m: MetricConfig): number | null | undefined =>
    m.key === 'activity' ? activityBurned : m.key === 'balance' ? balanceKcal : m.getValue(data)

  const tileClick = (m: MetricConfig) => {
    if (m.key === 'activity' || m.key === 'balance') { setBalanceContext(m.key === 'balance'); setSelected(m); return }
    setBalanceContext(false)
    setSelected(m)
  }

  const sparkFor = (m: MetricConfig): number[] | undefined => {
    if (m.key === 'activity') return [22, 40, 18, 55, 30, 64, 48]
    if (m.key === 'balance') return [-20, -35, -10, -45, -30, -50, -61]
    return undefined
  }

  const deltaFor = (m: MetricConfig): { text: string; dir: 'up' | 'down' | 'flat' } | undefined => {
    const v = tileValue(m)
    if (v == null) return undefined
    switch (m.key) {
      case 'calories': {
        const goal = goals['daily_calories'] ?? 2200
        const pct = Math.round((v / goal) * 100)
        return v <= goal
          ? { text: `Under goal · ${pct}%`, dir: 'up' }
          : { text: `Over goal · ${pct}%`, dir: 'down' }
      }
      case 'balance': {
        return v < 0
          ? { text: 'Deficit · on plan', dir: 'up' }
          : v > 0
            ? { text: 'Surplus', dir: 'down' }
            : { text: 'Balanced', dir: 'flat' }
      }
      case 'steps': {
        const goal = goals['daily_steps']
        if (!goal) return undefined
        return v >= goal
          ? { text: `Goal met · ${Math.round((v / goal) * 100)}%`, dir: 'up' }
          : { text: `${(goal - v).toLocaleString()} to goal`, dir: 'flat' }
      }
      case 'water': {
        const goal = goals['daily_water'] ?? 2000
        return v >= goal
          ? { text: 'Hydrated', dir: 'up' }
          : { text: `${Math.round(goal - v).toLocaleString()} ml to goal`, dir: 'flat' }
      }
      case 'sleep': {
        const goal = goals['daily_sleep'] ?? 7 * 60
        const diff = v - goal
        const h = Math.abs(Math.round(diff / 60 * 10) / 10)
        return diff >= 0
          ? { text: `${h}h over goal`, dir: 'up' }
          : { text: `−${h}h vs goal`, dir: 'down' }
      }
      case 'activity':
        return v > 0 ? { text: 'Activity logged', dir: 'up' } : undefined
      default:
        return undefined
    }
  }

  const renderTile = (m: MetricConfig) => (
    <MetricTile
      key={m.key}
      config={m}
      value={tileValue(m)}
      measuredAt={m.getMeasuredAt(data)}
      isStale={m.getIsStale(data)}
      daysOld={m.getDaysOld(data)}
      source={m.getSource(data)}
      spark={sparkFor(m)}
      delta={deltaFor(m)}
      missLabel={m.key === 'heart_rate' ? 'No data — 1d' : 'No data'}
      displayValue={m.key === 'blood_pressure' && data.latest_bp
        ? `${Math.round(data.latest_bp.systolic_mmhg)}/${Math.round(data.latest_bp.diastolic_mmhg)}`
        : undefined}
      breakdown={m.key === 'water' && data.liquids_today
        ? [
            { label: 'Water', ml: data.liquids_today.water },
            { label: 'Non-alcoholic', ml: data.liquids_today.non_alcoholic },
            { label: 'Beer', ml: data.liquids_today.beer },
            { label: 'Wine', ml: data.liquids_today.wine },
            { label: 'Spirits', ml: data.liquids_today.spirits },
            { label: 'Other alcohol', ml: data.liquids_today.other_alcohol },
          ].filter((b) => b.ml > 0)
        : undefined}
      onClick={() => tileClick(m)}
    />
  )

  return (
    <div className="home">
      <VerdictStrip data={data} />

      <h2 className="section-h">Today</h2>
      <div className="grid">
        {METRICS.filter((m) => HERO.includes(m.key)).map(renderTile)}
      </div>

      <h2 className="section-h">Vitals &amp; Fuel</h2>
      <div className="grid">
        {METRICS.filter((m) => VITALS.includes(m.key)).map(renderTile)}
      </div>

      {data.missing_data_note && <div className="note-strip">⚠ {data.missing_data_note}</div>}

      {selected && (
        <MetricDashboard
          config={selected}
          onClose={() => setSelected(null)}
          onSaved={reload}
          balanceContext={balanceContext}
        />
      )}
    </div>
  )
}