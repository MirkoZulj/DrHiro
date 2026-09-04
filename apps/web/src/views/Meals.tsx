import { useEffect, useState, useMemo, useCallback } from 'react'
import { authClient } from '../lib/auth'
import { Meal } from '../lib/types'
import { isoDate, titleCase } from '../lib/format'

type MealType = 'breakfast' | 'lunch' | 'dinner' | 'snack'

const MEAL_TYPES: MealType[] = ['breakfast', 'lunch', 'dinner', 'snack']
const DAY_LETTERS = ['M', 'T', 'W', 'T', 'F', 'S', 'S']

function mealTypeIcon(type: string): string {
  switch (type) {
    case 'breakfast': return '🍳'
    case 'lunch': return '🥗'
    case 'dinner': return '🍽️'
    case 'snack': return '🍎'
    default: return '🍴'
  }
}

/** Monday-based ISO week number and date range. */
function weekInfo(d: Date): { weekNum: number; weekLabel: string; days: Date[] } {
  const date = new Date(d)
  const day = (date.getDay() + 6) % 7 // 0=Mon
  const mon = new Date(date)
  mon.setDate(date.getDate() - day)
  const days: Date[] = []
  for (let i = 0; i < 7; i++) {
    const dd = new Date(mon)
    dd.setDate(mon.getDate() + i)
    days.push(dd)
  }
  // ISO week number
  const utcDate = new Date(Date.UTC(mon.getFullYear(), mon.getMonth(), mon.getDate()))
  const dayNum = utcDate.getUTCDay() || 7
  utcDate.setUTCDate(utcDate.getUTCDate() + 4 - dayNum)
  const yearStart = new Date(Date.UTC(utcDate.getUTCFullYear(), 0, 1))
  const weekNum = Math.ceil(((utcDate.getTime() - yearStart.getTime()) / 86400000 + 1) / 7)
  const sun = days[6]
  const fmt = (x: Date) => x.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  return { weekNum, weekLabel: `Week ${weekNum} · ${fmt(mon)} – ${fmt(sun)}`, days }
}

export default function Meals() {
  const [selectedDate, setSelectedDate] = useState(new Date())
  const { weekLabel, days } = useMemo(() => weekInfo(selectedDate), [selectedDate])
  const [weekMeals, setWeekMeals] = useState<Record<string, Meal[]>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<'meals' | 'drinks'>('meals')
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set(['breakfast']))

  const dateStr = useMemo(() => isoDate(selectedDate), [selectedDate])
  const todayStr = useMemo(() => isoDate(), [])

  const loadWeek = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const from = isoDate(days[0])
      const to = isoDate(days[6])
      const meals = await authClient.api(`/meals?from=${from}&to=${to}`)
      const arr: Meal[] = Array.isArray(meals) ? meals : []
      const map: Record<string, Meal[]> = {}
      for (const d of days) map[isoDate(d)] = []
      for (const m of arr) {
        const d = m.eaten_at?.slice(0, 10) || ''
        if (!map[d]) map[d] = []
        map[d].push(m)
      }
      setWeekMeals(map)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => { loadWeek() }, [loadWeek])

  const dayMeals = useMemo(() => weekMeals[dateStr] ?? [], [weekMeals, dateStr])
  const dayTotals = useMemo(() => {
    let kcal = 0, count = 0
    for (const m of dayMeals) {
      kcal += m.totals_json?.kcal ?? 0
      count++
    }
    return { kcal: Math.round(kcal), count }
  }, [dayMeals])

  const weekTotals = useMemo(() => {
    let total = 0, daysWithData = 0
    for (const d of days) {
      const meals = weekMeals[isoDate(d)] ?? []
      let dayKcal = 0
      for (const m of meals) dayKcal += m.totals_json?.kcal ?? 0
      if (dayKcal > 0) daysWithData++
      total += dayKcal
    }
    return { total: Math.round(total), avg: daysWithData ? Math.round(total / daysWithData) : 0 }
  }, [weekMeals, days])

  const dayKcalMap = useMemo(() => {
    const m: Record<string, number> = {}
    for (const d of days) {
      const meals = weekMeals[isoDate(d)] ?? []
      let kcal = 0
      for (const mm of meals) kcal += mm.totals_json?.kcal ?? 0
      m[isoDate(d)] = Math.round(kcal)
    }
    return m
  }, [weekMeals, days])

  const mealsByType = useMemo(() => {
    const map: Record<string, Meal[]> = {}
    for (const t of MEAL_TYPES) map[t] = []
    for (const m of dayMeals) {
      const t = (m.meal_type || 'snack') as MealType
      if (!map[t]) map[t] = []
      map[t].push(m)
    }
    return map
  }, [dayMeals])

  const drinks = useMemo(() => {
    const result: Meal[] = []
    for (const m of dayMeals) {
      for (const item of m.items ?? []) {
        const name = (item.display_name || item.food_name || '').toLowerCase()
        if (item.unit === 'ml' || name.includes('water') || name.includes('coffee') || name.includes('tea') || name.includes('juice') || name.includes('drink')) {
          result.push(m)
          break
        }
      }
    }
    return result
  }, [dayMeals])

  function toggleGroup(type: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev)
      if (next.has(type)) next.delete(type)
      else next.add(type)
      return next
    })
  }

  function shiftWeek(delta: number) {
    const next = new Date(selectedDate)
    next.setDate(next.getDate() + delta * 7)
    setSelectedDate(next)
  }

  function selectDay(d: Date) {
    if (isoDate(d) <= todayStr) setSelectedDate(d)
  }

  const selectedDayName = selectedDate.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })

  return (
    <div className="meals-view">
      {/* Header */}
      <div className="meals-header">
        <div>
          <h2>Meals</h2>
          <p className="page-sub">{selectedDayName} · {dayTotals.count} meals · {dayTotals.kcal.toLocaleString()} kcal</p>
        </div>
        <div className="meals-header-right">
          <div className="mini-tabs" role="tablist">
            <button className={`mini-tab${activeTab === 'meals' ? ' active' : ''}`} onClick={() => setActiveTab('meals')}>Meals</button>
            <button className={`mini-tab${activeTab === 'drinks' ? ' active' : ''}`} onClick={() => setActiveTab('drinks')}>Drinks</button>
          </div>
          <button className="week-nav" onClick={() => shiftWeek(-1)} aria-label="Previous week">‹</button>
          <span className="week-nav-label">{weekLabel}</span>
          <button className="week-nav" onClick={() => shiftWeek(1)} aria-label="Next week">›</button>
        </div>
      </div>

      {/* Day selector */}
      <div className="day-selector">
        {days.map((d, i) => {
          const ds = isoDate(d)
          const sel = ds === dateStr
          const future = ds > todayStr
          const kcal = dayKcalMap[ds] ?? 0
          return (
            <button
              key={ds}
              className={`day-btn${sel ? ' active' : ''}${future ? ' future' : ''}`}
              disabled={future}
              onClick={() => selectDay(d)}
            >
              <span className="day-letter">{DAY_LETTERS[i]}</span>
              <span className="day-num">{d.getDate()}</span>
              <span className="day-kcal">{kcal > 0 ? `${kcal}` : '—'}</span>
            </button>
          )
        })}
      </div>

      {/* Week strip */}
      <div className="week-strip">
        <div className="week-strip-stats">
          <span className="week-total">{weekTotals.total.toLocaleString()} kcal total</span>
          <span className="week-avg">{weekTotals.avg.toLocaleString()} avg/day</span>
        </div>
        <div className="week-bars">
          {days.map((d) => {
            const ds = isoDate(d)
            const kcal = dayKcalMap[ds] ?? 0
            const maxKcal = Math.max(...Object.values(dayKcalMap), 1)
            const pct = kcal > 0 ? (kcal / maxKcal) * 100 : 0
            const missing = kcal === 0
            return (
              <div key={ds} className="week-bar-col">
                <div className={`week-bar${missing ? ' missing' : ''}`} style={{ height: `${Math.max(pct, 4)}%` }} />
                <span className="week-bar-label">{DAY_LETTERS[days.indexOf(d)]}</span>
              </div>
            )
          })}
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {loading ? (
        <div className="loading">Loading…</div>
      ) : activeTab === 'meals' ? (
        <div className="meal-groups">
          {MEAL_TYPES.map((type) => {
            const items = mealsByType[type] ?? []
            const groupKcal = items.reduce((sum, m) => sum + (m.totals_json?.kcal ?? 0), 0)
            const isExpanded = expandedGroups.has(type)
            return (
              <div key={type} className="meal-group">
                <button
                  className={`meal-group-header${isExpanded ? ' expanded' : ''}`}
                  onClick={() => toggleGroup(type)}
                  aria-expanded={isExpanded}
                >
                  <span className="meal-group-icon">{mealTypeIcon(type)}</span>
                  <span className="meal-group-label">{titleCase(type)}</span>
                  <span className="meal-group-kcal">{Math.round(groupKcal)} kcal</span>
                  <span className="meal-group-count">{items.length}</span>
                  <span className={`meal-group-chevron${isExpanded ? ' open' : ''}`}>▾</span>
                </button>
                {isExpanded && (
                  <div className="meal-group-body">
                    {items.length === 0 ? (
                      <div className="empty">Nothing logged</div>
                    ) : (
                      <ul className="meal-item-list">
                        {items.flatMap((m) =>
                          (m.items ?? []).map((item, idx) => {
                            const n = item.nutrients_json ?? {}
                            const itemKcal =
                              typeof n.kcal === 'number' ? Math.round(n.kcal) :
                              typeof n.calories === 'number' ? Math.round(n.calories) : null
                            const isDrink = item.unit === 'ml' || (item.display_name || item.food_name || '').toLowerCase().includes('water') || (item.display_name || item.food_name || '').toLowerCase().includes('coffee') || (item.display_name || item.food_name || '').toLowerCase().includes('tea')
                            return (
                              <li key={item.id ?? `${m.id}-${idx}`} className="meal-item">
                                <div className="meal-item-main">
                                  <span className="meal-item-name">
                                    {item.display_name || item.food_name || 'Unknown'}
                                    {isDrink && <span className="drink-tag">DRINK</span>}
                                  </span>
                                  <span className="meal-item-grams">{item.grams}g</span>
                                </div>
                                {itemKcal != null && <span className="meal-item-kcal">{itemKcal} kcal</span>}
                              </li>
                            )
                          })
                        )}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      ) : (
        <div className="drinks-list">
          {drinks.length === 0 ? (
            <div className="empty">No drinks logged.</div>
          ) : (
            drinks.map((m) => (
              <div key={m.id} className="drink-row">
                <span className="drink-icon">💧</span>
                <div className="drink-info">
                  <span className="drink-name">{m.items?.[0]?.display_name || m.items?.[0]?.food_name || 'Drink'}</span>
                  <span className="drink-amount">{m.items?.[0]?.grams} ml</span>
                </div>
                <span className="drink-kcal">{Math.round(m.totals_json?.kcal ?? 0)} kcal</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}