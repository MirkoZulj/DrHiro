import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'
import { MetricConfig, TrendData, ManualTextResult } from '../lib/types'
import { fmtDate } from '../lib/format'

interface NutritionPoint {
  date: string
  kcal: number
  protein_g: number
  carbs_g: number
  fat_g: number
  meals_count: number
}

interface NutritionTrend {
  points: NutritionPoint[]
  days: number
}

interface MetricPopupProps {
  config: MetricConfig
  onClose: () => void
  onSaved: () => void
}

export default function MetricPopup({ config, onClose, onSaved }: MetricPopupProps) {
  const [weekly, setWeekly] = useState<TrendData | null>(null)
  const [monthly, setMonthly] = useState<TrendData | null>(null)
  const [loading, setLoading] = useState(true)
  const [entryText, setEntryText] = useState('')
  const [entryResult, setEntryResult] = useState<ManualTextResult | null>(null)
  const [entryError, setEntryError] = useState<string | null>(null)
  const [entryBusy, setEntryBusy] = useState(false)
  const [monthlySummary, setMonthlySummary] = useState<any>(null)
  const [nutritionTrend, setNutritionTrend] = useState<NutritionTrend | null>(null)
  const [timeframe, setTimeframe] = useState<'D' | 'W' | 'M'>('W')
  const [dailyGoal, setDailyGoal] = useState<number | null>(null)

  useEffect(() => {
    const goalTypeMap: Record<string, string> = {
      steps: 'daily_steps',
      calories: 'daily_calories',
      water: 'daily_water',
      sleep: 'daily_sleep',
      activity: 'daily_activity',
    }
    const gt = goalTypeMap[config.key]
    if (!gt) return
    authClient.api('/goals').then((list) => {
      const arr = Array.isArray(list) ? list : []
      const g = arr.find((x: any) => x.goal_type === gt)
      setDailyGoal(g?.target_json?.target ?? null)
    }).catch(() => {})
  }, [config.key])

  const isCalories = config.trendKey === 'calories_kcal' || config.key === 'calories'

  useEffect(() => {
    setLoading(true)
    const requests: Promise<any>[] = [
      isCalories ? authClient.api(`/trends?metric=weight&period=7d`).catch(() => null) : authClient.api(`/trends/daily/${config.trendKey}?days=7`).catch(() => null),
      isCalories ? authClient.api(`/trends?metric=weight&period=30d`).catch(() => null) : authClient.api(`/trends/daily/${config.trendKey}?days=30`).catch(() => null),
      authClient.api(`/summaries/monthly/${new Date().toISOString().slice(0, 7)}`).catch(() => null),
    ]
    if (isCalories) {
      requests.push(
        authClient.api('/trends?metric=calories&period=7d').catch(() => null)
      )
    }
    Promise.all(requests).then(([w, m, month, ...rest]) => {
      setWeekly(w)
      setMonthly(m)
      if (month) setMonthlySummary(month)
      if (isCalories) {
        const [nutrition] = rest
        if (nutrition) setNutritionTrend(nutrition)
      }
      setLoading(false)
    })
  }, [config.trendKey, isCalories])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  async function submitEntry() {
    if (!entryText.trim()) return
    setEntryError(null)
    setEntryResult(null)
    setEntryBusy(true)
    try {
      const res = await authClient.api('/ingest/manual/text', {
        method: 'POST',
        body: JSON.stringify({ text: entryText.trim() }),
      })
      setEntryResult(res)
      if (res && res.accepted > 0) {
        setEntryText('')
        onSaved()
      }
    } catch (e) {
      const msg = String(e)
      if (msg.includes('422')) {
        setEntryError('Nothing could be parsed from that text. Try something like "78.5 kg" or "120/80".')
      } else {
        setEntryError(msg)
      }
    } finally {
      setEntryBusy(false)
    }
  }

  const analytics = generateAnalytics(config, weekly, monthly, nutritionTrend)
  const weeklyBars = weekly?.points ?? []
  const monthlyBars = monthly?.points ?? []
  const weeklyMax = Math.max(...weeklyBars.map((p) => p.value), 1)
  const monthlyMax = Math.max(...monthlyBars.map((p) => p.value), 1)
  const nutritionPoints = nutritionTrend?.points ?? []

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={`${config.label} details`}>
        <div className="modal-header">
          <div className="modal-title-row">
            <span className="modal-icon" style={{ color: 'var(--metric-color)' }}>{config.icon}</span>
            <h2>{config.label}</h2>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="modal-content">
          {loading ? (
            <div className="loading">Loading trends…</div>
          ) : (
            <>
              {!isCalories && (
                <div className="tf-switch" role="group" aria-label="Timeframe">
                  {(['D', 'W', 'M'] as const).map((tf) => (
                    <button
                      key={tf}
                      className={`tf-btn${timeframe === tf ? ' active' : ''}`}
                      onClick={() => setTimeframe(tf)}
                    >{tf}</button>
                  ))}
                </div>
              )}

              {/* ===== CALORIES / NUTRITION DETAIL ===== */}

              {/* ===== INSIGHTS ===== */}
              {analytics.length > 0 && (
                <section className="analytics-section">
                  <h3>Insights</h3>
                  <ul className="analytics-list">
                    {analytics.map((a, i) => (
                      <li key={i} className={`insight insight-${a.severity}`}>
                        <span className="insight-icon">{a.icon}</span>
                        <span className="insight-text">{a.text}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {/* ===== MACRO CHARTS (Calories only) ===== */}
              {isCalories && nutritionPoints.length >= 2 && (
                <section className="section">
                  <h3>Macros — last {nutritionPoints.length} days</h3>
                  <MacroChart points={nutritionPoints} />
                </section>
              )}

              {/* ===== WEIGHT MONTHLY SUMMARY ===== */}
              {monthlySummary?.weight && config.trendKey === 'weight_kg' && (
                <p className="text-xs text-[var(--color-text-muted)] mt-2">
                  Weight change this month: <b>{monthlySummary.weight.delta_kg > 0 ? '+' : ''}{monthlySummary.weight.delta_kg} kg</b> across {monthlySummary.weight.samples} entries.
                </p>
              )}
              {monthlySummary?.blood_pressure && config.trendKey === 'blood_pressure_systolic' && (
                <p className="text-xs text-[var(--color-text-muted)] mt-2">
                  BP avg this month: <b>{monthlySummary.blood_pressure.avg_systolic}/{monthlySummary.blood_pressure.avg_diastolic}</b> mmHg across {monthlySummary.blood_pressure.samples} readings.
                </p>
              )}

              {/* ===== TREND BARS (D/W/M) ===== */}
              {!isCalories && weeklyBars.length > 0 && timeframe !== 'M' && (
                <section className="section">
                  <h3>{timeframe === 'D' ? 'Daily — last 7 days' : 'Weekly view — last 7 days'}{dailyGoal != null ? ` — goal ${dailyGoal.toLocaleString()} ${config.unit}` : ''}</h3>
                  <div className="card">
                    <div className="bars" style={{ position: 'relative' }}>
                      {weeklyBars.map((p) => (
                        <div
                          key={p.date}
                          className="bar"
                          style={{ height: `${(p.value / weeklyMax) * 100}%`, backgroundColor: 'var(--metric-color)' }}
                          title={`${p.value} ${config.unit} on ${p.date}`}
                        />
                      ))}
                      {dailyGoal != null && dailyGoal <= weeklyMax && (
                        <div
                          className="goal-line"
                          style={{ bottom: `${(dailyGoal / weeklyMax) * 100}%` }}
                          title={`Goal: ${dailyGoal} ${config.unit}`}
                        />
                      )}
                    </div>
                    <div className="bar-axis">
                      {weeklyBars.map((p) => (
                        <span key={p.date}>{fmtDate(p.date)}</span>
                      ))}
                    </div>
                  </div>
                </section>
              )}

              {/* ===== 30-DAY BARS ===== */}
              {!isCalories && monthlyBars.length > 0 && timeframe === 'M' && (
                <section className="section">
                  <h3>Monthly view — last 30 days{dailyGoal != null ? ` — goal ${dailyGoal.toLocaleString()} ${config.unit}` : ''}</h3>
                  <div className="card">
                    <div className="bars bars-monthly" style={{ position: 'relative' }}>
                      {monthlyBars.map((p) => (
                        <div
                          key={p.date}
                          className="bar"
                          style={{ height: `${(p.value / monthlyMax) * 100}%`, backgroundColor: 'var(--metric-color)', opacity: 0.7 }}
                          title={`${p.value} ${config.unit} on ${p.date}`}
                        />
                      ))}
                      {dailyGoal != null && dailyGoal <= monthlyMax && (
                        <div
                          className="goal-line"
                          style={{ bottom: `${(dailyGoal / monthlyMax) * 100}%` }}
                          title={`Goal: ${dailyGoal} ${config.unit}`}
                        />
                      )}
                    </div>
                    <div className="bar-axis">
                      {monthlyBars.filter((_, i) => i % 5 === 0).map((p) => (
                        <span key={p.date}>{fmtDate(p.date)}</span>
                      ))}
                    </div>
                  </div>
                </section>
              )}

              {weeklyBars.length === 0 && monthlyBars.length === 0 && !isCalories && (
                <div className="empty" style={{ marginTop: 16 }}>No trend data available yet.</div>
              )}
            </>
          )}

          {/* ===== QUICK ENTRY ===== */}
          {!isCalories && (
            <section className="section manual-entry-section">
            <h3>Quick entry</h3>
            <div className="quick-entry-row">
              <input
                type="text"
                value={entryText}
                onChange={(e) => setEntryText(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') submitEntry() }}
                placeholder={`e.g. ${config.key === 'weight' ? '78.5 kg' : config.key === 'blood_pressure' ? '120/80' : config.key === 'water' ? '2.5 liters' : 'value'}`}
              />
              <button onClick={submitEntry} disabled={entryBusy || !entryText.trim()} className="small">
                {entryBusy ? <span className="spinner" /> : 'Log'}
              </button>
            </div>
            {entryError && <div className="error" style={{ marginTop: 8 }}>{entryError}</div>}
            {entryResult && entryResult.accepted > 0 && (
              <div className="status" style={{ marginTop: 8 }}>Saved {entryResult.accepted} measurement(s).</div>
            )}
            <p className="muted" style={{ marginTop: 4 }}>
              Type a measurement like "78.5 kg", "120/80", or "2.5 liters of water".
            </p>
          </section>
            )}
        </div>
      </div>
    </div>
  )
}

/* ===== Macro Chart Component ===== */
function MacroChart({ points }: { points: NutritionPoint[] }) {
  const maxKcal = Math.max(...points.map((p) => p.kcal), 1)
  const maxMacro = Math.max(...points.flatMap((p) => [p.protein_g, p.carbs_g, p.fat_g]), 1)

  return (
    <div className="macro-chart">
      <div className="macro-legend">
        <span className="legend-item protein">Protein</span>
        <span className="legend-item carbs">Carbs</span>
        <span className="legend-item fat">Fat</span>
        <span className="legend-item kcal">kcal (line)</span>
      </div>
      <div className="macro-bars">
        {points.map((p) => {
          const pPct = (p.protein_g / maxMacro) * 100
          const cPct = (p.carbs_g / maxMacro) * 100
          const fPct = (p.fat_g / maxMacro) * 100
          const kcalPct = (p.kcal / maxKcal) * 100
          return (
            <div key={p.date} className="macro-bar-group">
              <div className="macro-bar-stack">
                <div className="macro-bar protein" style={{ height: `${pPct}%` }} title={`P: ${Math.round(p.protein_g)}g`} />
                <div className="macro-bar carbs" style={{ height: `${cPct}%` }} title={`C: ${Math.round(p.carbs_g)}g`} />
                <div className="macro-bar fat" style={{ height: `${fPct}%` }} title={`F: ${Math.round(p.fat_g)}g`} />
                <div className="kcal-line" style={{ bottom: `${kcalPct}%` }} />
              </div>
              <span className="macro-bar-label">{fmtDate(p.date)}</span>
              <span className="macro-bar-kcal">{Math.round(p.kcal)}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ===== Analytics ===== */
interface Insight {
  text: string
  icon: string
  severity: 'info' | 'warning' | 'success' | 'neutral'
}

function generateAnalytics(config: MetricConfig, weekly: TrendData | null, monthly: TrendData | null, nutritionTrend: NutritionTrend | null): Insight[] {
  const insights: Insight[] = []
  const points = weekly?.points ?? []
  const isCalories = config.trendKey === 'calories_kcal' || config.key === 'calories'

  // Calories-specific insights from nutrition trend
  if (isCalories && nutritionTrend && nutritionTrend.points.length > 0) {
    const np = nutritionTrend.points
    const recentDays = np.slice(-3)
    const olderDays = np.slice(-7, -3)
    const recentAvgKcal = recentDays.reduce((s, p) => s + p.kcal, 0) / recentDays.length
    const recentAvgProtein = recentDays.reduce((s, p) => s + p.protein_g, 0) / recentDays.length
    const recentAvgCarbs = recentDays.reduce((s, p) => s + p.carbs_g, 0) / recentDays.length
    const recentAvgFat = recentDays.reduce((s, p) => s + p.fat_g, 0) / recentDays.length

    insights.push({
      text: `3-day avg: ${Math.round(recentAvgKcal)} kcal · P:${Math.round(recentAvgProtein)}g · C:${Math.round(recentAvgCarbs)}g · F:${Math.round(recentAvgFat)}g`,
      icon: '📊',
      severity: 'info',
    })

    if (olderDays.length > 0) {
      const olderAvgKcal = olderDays.reduce((s, p) => s + p.kcal, 0) / olderDays.length
      const kcalDiff = recentAvgKcal - olderAvgKcal
      if (Math.abs(kcalDiff) > 200) {
        insights.push({
          text: `Calorie intake ${kcalDiff > 0 ? 'up' : 'down'} ${Math.round(Math.abs(kcalDiff))} kcal vs previous days.`,
          icon: kcalDiff > 0 ? '📈' : '📉',
          severity: 'info',
        })
      }
    }

    // Macro balance
    if (recentAvgProtein + recentAvgCarbs + recentAvgFat > 0) {
      const pPct = Math.round((recentAvgProtein / (recentAvgProtein + recentAvgCarbs + recentAvgFat)) * 100)
      const cPct = Math.round((recentAvgCarbs / (recentAvgProtein + recentAvgCarbs + recentAvgFat)) * 100)
      const fPct = 100 - pPct - cPct
      insights.push({
        text: `Macro split: ${pPct}% protein · ${cPct}% carbs · ${fPct}% fat`,
        icon: '🥗',
        severity: 'info',
      })
    }

    // Meal frequency
    const avgMeals = np.reduce((s, p) => s + p.meals_count, 0) / np.length
    if (avgMeals < 2) {
      insights.push({ text: `Only ${avgMeals.toFixed(1)} meals/day on average. Consider eating more regularly.`, icon: '🍽️', severity: 'warning' })
    } else if (avgMeals >= 3) {
      insights.push({ text: `${avgMeals.toFixed(1)} meals/day — good eating frequency.`, icon: '✅', severity: 'success' })
    }
    return insights
  }

  if (points.length === 0) {
    insights.push({ text: `No data yet for ${config.label.toLowerCase()}. Start logging to see insights.`, icon: '📊', severity: 'neutral' })
    return insights
  }

  const values = points.map((p) => p.value)
  const latest = values[values.length - 1]
  const prev = values.length > 1 ? values[values.length - 2] : null
  const avg = values.reduce((a, b) => a + b, 0) / values.length

  if (values.length >= 3) {
    const recent3 = values.slice(-3)
    const declining = recent3.every((v, i) => i === 0 || v <= recent3[i - 1])
    const increasing = recent3.every((v, i) => i === 0 || v >= recent3[i - 1])
    if (declining) {
      insights.push({ text: `${config.label} has been declining for ${Math.min(3, values.length)} days in a row.`, icon: '📉', severity: 'warning' })
    } else if (increasing) {
      insights.push({ text: `${config.label} has been increasing for ${Math.min(3, values.length)} days in a row.`, icon: '📈', severity: 'success' })
    }
  }

  if (prev != null && latest != null) {
    const diff = latest - prev
    if (config.key === 'blood_pressure') {
      if (latest < 120) insights.push({ text: `Your latest ${config.label.toLowerCase()} (${Math.round(latest)} mmHg systolic) is in the normal range.`, icon: '✅', severity: 'success' })
      else if (latest < 130) insights.push({ text: `Your latest ${config.label.toLowerCase()} (${Math.round(latest)} mmHg systolic) is elevated. Consider lifestyle changes.`, icon: '⚠️', severity: 'warning' })
      else if (latest >= 130) insights.push({ text: `Your latest ${config.label.toLowerCase()} (${Math.round(latest)} mmHg systolic) is high. Consider consulting a healthcare professional.`, icon: '🔴', severity: 'warning' })
    } else if (config.key === 'steps') {
      if (latest >= 10000) insights.push({ text: `Great! You've reached 10,000+ steps today.`, icon: '🎯', severity: 'success' })
      else if (latest >= avg * 0.8) insights.push({ text: `You're on track with ${config.label.toLowerCase()} — close to your 7-day average of ${Math.round(avg)}.`, icon: '👍', severity: 'info' })
      else insights.push({ text: `Your ${config.label.toLowerCase()} is below your 7-day average of ${Math.round(avg)}. Every step counts!`, icon: '💡', severity: 'info' })
    } else if (config.key === 'weight') {
      if (Math.abs(diff) > 0) insights.push({ text: `Weight changed by ${diff > 0 ? '+' : ''}${diff.toFixed(1)} kg since last reading.`, icon: '⚖️', severity: 'info' })
      const monthlyPoints = monthly?.points ?? []
      if (monthlyPoints.length >= 2) {
        const monthDelta = monthlyPoints[monthlyPoints.length - 1].value - monthlyPoints[0].value
        if (Math.abs(monthDelta) >= 1) insights.push({ text: `Over 30 days, weight ${monthDelta > 0 ? 'increased' : 'decreased'} by ${Math.abs(monthDelta).toFixed(1)} kg.`, icon: '📊', severity: 'info' })
      }
    } else if (config.key === 'water') {
      if (latest < 1500) insights.push({ text: `You've logged only ${Math.round(latest)} ml today. Aim for at least 2 liters!`, icon: '💧', severity: 'warning' })
      else if (latest >= 2000) insights.push({ text: `Well hydrated! You've logged ${Math.round(latest)} ml today.`, icon: '💧', severity: 'success' })
    } else if (config.key === 'sleep') {
      const hours = latest / 60
      if (hours < 6) insights.push({ text: `Only ${hours.toFixed(1)}h of sleep — adults need 7-9 hours.`, icon: '😴', severity: 'warning' })
      else if (hours >= 7 && hours <= 9) insights.push({ text: `${hours.toFixed(1)}h of sleep — that's in the healthy range.`, icon: '🌙', severity: 'success' })
    } else if (config.key === 'heart_rate') {
      if (latest >= 60 && latest <= 100) insights.push({ text: `Resting heart rate of ${Math.round(latest)} bpm is in the normal range (60-100).`, icon: '❤️', severity: 'success' })
      else if (latest > 100) insights.push({ text: `Resting heart rate of ${Math.round(latest)} bpm is above 100 — consider consulting a doctor if persistent.`, icon: '❤️', severity: 'warning' })
    }
  }

  if (values.length < 7) {
    const missing = 7 - values.length
    if (missing >= 3) insights.push({ text: `You haven't logged ${config.label.toLowerCase()} for ${missing} of the last 7 days. Regular tracking gives better insights.`, icon: '📅', severity: 'neutral' })
  }

  if (insights.length === 0) {
    insights.push({ text: `Looking steady! Keep logging ${config.label.toLowerCase()} for more insights.`, icon: '✨', severity: 'neutral' })
  }

  return insights
}

