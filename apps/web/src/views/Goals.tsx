import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'
import { Goal, GoalCreate } from '../lib/types'
import { fmtDate, titleCase, isoDate } from '../lib/format'

const GOAL_TYPES = ['weight_loss', 'weight_gain', 'weight_maintain']

interface Plan {
  summary: string
  direction: 'loss' | 'gain' | 'maintain'
  calories: string
  exercise: string
}

/** Build a client-side plan for a weight goal given a target delta (kg). */
function buildPlan(deltaKg: number): Plan {
  // deltaKg > 0 means losing weight (start > target).
  if (deltaKg > 0) {
    return {
      summary: `Lose about ${Math.abs(deltaKg).toFixed(1)} kg at a steady, sustainable pace.`,
      direction: 'loss',
      calories: 'Trim roughly 500 kcal per day from your usual intake (a moderate, safe deficit).',
      exercise: 'Add 150–250 minutes of moderate exercise per week (e.g. brisk walks or cycling) and 2 strength sessions.',
    }
  }
  if (deltaKg < 0) {
    return {
      summary: `Gain about ${Math.abs(deltaKg).toFixed(1)} kg in a controlled, healthy way.`,
      direction: 'gain',
      calories: 'Add roughly 300–500 kcal per day, favouring protein- and nutrient-dense foods.',
      exercise: 'Focus on 2–3 resistance-training sessions per week to build lean mass, with light cardio.',
    }
  }
  return {
    summary: 'Hold your current weight steady with consistent daily habits.',
    direction: 'maintain',
    calories: 'Keep intake balanced to roughly match your daily energy expenditure.',
    exercise: 'Maintain regular activity — about 150 minutes per week — to stay consistent.',
  }
}

/** Derive the delta (start → target) from a goal's target_json. */
function planFromGoal(g: Goal): Plan | null {
  const t = (g.target_json ?? {}) as Record<string, unknown>
  const start = typeof t.start_kg === 'number' ? t.start_kg : typeof t.current_kg === 'number' ? t.current_kg : null
  const target = typeof t.target_kg === 'number' ? t.target_kg : null
  if (start == null || target == null) return null
  return buildPlan(start - target)
}

function renderTarget(g: Goal): string {
  const t = g.target_json ?? {}
  const keys = Object.keys(t)
  if (keys.length === 0) return '—'
  return keys.map((k) => `${k}: ${String((t as Record<string, unknown>)[k])}`).join(', ')
}

export default function Goals() {
  const [goals, setGoals] = useState<Goal[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // create form
  const [goalType, setGoalType] = useState('weight_loss')
  const [startKg, setStartKg] = useState('')
  const [targetKg, setTargetKg] = useState('')
  const [endDate, setEndDate] = useState('')
  const [creating, setCreating] = useState(false)
  const [createdMsg, setCreatedMsg] = useState('')

  // per-tile daily goals
  const [tileGoals, setTileGoals] = useState<Record<string, number | null>>({})
  const [goalDrafts, setGoalDrafts] = useState<Record<string, string>>({})
  const [goalMsg, setGoalMsg] = useState('')

  const TILE_GOALS: { key: string; label: string; unit: string }[] = [
    { key: 'steps', label: 'Steps', unit: 'steps' },
    { key: 'calories', label: 'Calories', unit: 'kcal' },
    { key: 'water', label: 'Water', unit: 'ml' },
    { key: 'sleep', label: 'Sleep', unit: 'min' },
    { key: 'activity', label: 'Activity burn', unit: 'kcal' },
  ]

  async function loadTileGoals() {
    try {
      const list = await authClient.api('/goals')
      const arr = Array.isArray(list) ? list : []
      const map: Record<string, number | null> = {}
      for (const t of TILE_GOALS) {
        const g = arr.find((x: any) => x.goal_type === `daily_${t.key}`)
        map[t.key] = g ? (g.target_json?.target ?? null) : null
      }
      setTileGoals(map)
    } catch { /* ignore */ }
  }

  async function saveTileGoal(key: string) {
    const raw = goalDrafts[key]
    const v = raw === '' ? null : Number(raw)
    setGoalMsg('')
    try {
      if (v != null && Number.isFinite(v) && v > 0) {
        await authClient.api('/goals', {
          method: 'POST',
          body: JSON.stringify({ goal_type: `daily_${key}`, target_json: { target: v }, start_date: isoDate() }),
        })
        setGoalMsg(`${key} goal saved: ${v}`)
      } else {
        setGoalMsg(`${key} goal cleared (enter a number to set).`)
      }
      await loadTileGoals()
    } catch (e) { setGoalMsg(String(e)) }
  }

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const list = await authClient.api('/goals')
      setGoals(Array.isArray(list) ? list : [])
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    loadTileGoals()
  }, [])

  async function createGoal() {
    const start = Number(startKg)
    const target = Number(targetKg)
    if (Number.isNaN(start) || start <= 0) return setError('Enter a valid starting weight (kg).')
    if (Number.isNaN(target) || target <= 0) return setError('Enter a valid target weight (kg).')

    setCreating(true)
    setError(null)
    setCreatedMsg('')
    try {
      const body: GoalCreate = {
        goal_type: goalType,
        target_json: { start_kg: start, target_kg: target },
        start_date: isoDate(),
        end_date: endDate || null,
      }
      await authClient.api('/goals', { method: 'POST', body: JSON.stringify(body) })
      setStartKg('')
      setTargetKg('')
      setEndDate('')
      setCreatedMsg('Goal created — see your plan below.')
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const active = goals.filter((g) => g.status === 'active' || g.status === 'in_progress')
  const others = goals.filter((g) => !active.includes(g))

  // Preview plan while filling the form
  const start = Number(startKg)
  const target = Number(targetKg)
  const previewPlan = !Number.isNaN(start) && !Number.isNaN(target) && start > 0 && target > 0 ? buildPlan(start - target) : null

  return (
    <div className="goals">
      <h2>Goals</h2>
      <p className="page-sub">Set a health goal and get a plan to reach it.</p>

      {error && <div className="error">{error}</div>}
      {createdMsg && <div className="status">{createdMsg}</div>}

      <section className="section">
        <h3>Create a goal</h3>
        <div className="form-card">
          <div className="field">
            <label>Goal</label>
            <select value={goalType} onChange={(e) => setGoalType(e.target.value)}>
              {GOAL_TYPES.map((t) => (
                <option key={t} value={t}>
                  {titleCase(t.replace('_', ' '))}
                </option>
              ))}
            </select>
          </div>
          <div className="field-row">
            <div className="field">
              <label>Current weight (kg)</label>
              <input type="number" step="0.1" value={startKg} onChange={(e) => setStartKg(e.target.value)} placeholder="e.g. 78.5" />
            </div>
            <div className="field">
              <label>Target weight (kg)</label>
              <input type="number" step="0.1" value={targetKg} onChange={(e) => setTargetKg(e.target.value)} placeholder="e.g. 73.5" />
            </div>
          </div>
          <div className="field">
            <label>Target date (optional)</label>
            <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
          </div>
          {previewPlan && (
            <PlanCard plan={previewPlan} title="Your plan" />
          )}
          <div className="form-actions">
            <button onClick={createGoal} disabled={creating || !startKg || !targetKg}>
              {creating ? 'Creating…' : 'Create goal'}
            </button>
          </div>
        </div>
      </section>

      <section className="section">
        <h3>Daily tile goals</h3>
        <p className="page-sub">Shown as a target line in each tile's analytics chart.</p>
        {goalMsg && <div className="status">{goalMsg}</div>}
        <div className="form-card">
          {TILE_GOALS.map((t) => (
            <div className="field-row" key={t.key}>
              <div className="field">
                <label>{t.label} ({t.unit}) {tileGoals[t.key] != null ? `— current: ${tileGoals[t.key]}` : ''}</label>
                <input
                  type="number"
                  value={goalDrafts[t.key] ?? ''}
                  onChange={(e) => setGoalDrafts({ ...goalDrafts, [t.key]: e.target.value })}
                  placeholder={tileGoals[t.key] != null ? String(tileGoals[t.key]) : 'not set'}
                />
              </div>
              <div className="field" style={{ alignSelf: 'flex-end' }}>
                <button onClick={() => saveTileGoal(t.key)}>Save</button>
              </div>
            </div>
          ))}
        </div>
      </section>

      {loading ? (
        <div className="loading">Loading…</div>
      ) : (
        <>
          <section className="section">
            <h3>Active</h3>
            {active.length === 0 ? (
              <div className="empty">No active goals.</div>
            ) : (
              <ul>
                {active.map((g) => {
                  const plan = planFromGoal(g)
                  return (
                    <li key={g.id} className="list-item">
                      <div className="main">
                        <div className="title">{titleCase(g.goal_type)}</div>
                        <div className="desc">Target: {renderTarget(g)}</div>
                        <div className="desc">
                          {fmtDate(g.start_date)} → {g.end_date ? fmtDate(g.end_date) : 'ongoing'}
                        </div>
                        {plan && <PlanCard plan={plan} title="Your plan" />}
                      </div>
                      <span className="badge on">{g.status}</span>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>

          {others.length > 0 && (
            <section className="section">
              <h3>Past / other</h3>
              <ul>
                {others.map((g) => (
                  <li key={g.id} className="list-item">
                    <div className="main">
                      <div className="title">{titleCase(g.goal_type)}</div>
                      <div className="desc">Target: {renderTarget(g)}</div>
                    </div>
                    <span className="badge">{g.status}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  )
}

function PlanCard({ plan, title }: { plan: Plan; title: string }) {
  return (
    <div className="plan-card">
      <div className="plan-title">{title}</div>
      <div className="plan-summary">{plan.summary}</div>
      <ul className="plan-points">
        <li><span className="plan-bullet calories">Calories</span> {plan.calories}</li>
        <li><span className="plan-bullet exercise">Exercise</span> {plan.exercise}</li>
      </ul>
    </div>
  )
}
