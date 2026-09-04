import { useEffect, useState, useCallback } from 'react'
import { authClient } from '../lib/auth'
import { isoDate } from '../lib/format'

interface ActivityItem {
  id: string
  date: string
  title: string
  description: string | null
  calories_burned: number
}

function shiftDate(d: Date, days: number): Date {
  const next = new Date(d)
  next.setDate(next.getDate() + days)
  return next
}

export default function Activities() {
  const [date, setDate] = useState(new Date())
  const dateStr = isoDate(date)

  const [items, setItems] = useState<ActivityItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  // add form
  const [showForm, setShowForm] = useState(false)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [kcal, setKcal] = useState('')
  const [formDate, setFormDate] = useState(dateStr)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await authClient.api(`/activities?date=${dateStr}`)
      setItems(res.items ?? [])
      setTotal(res.total_burned ?? 0)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [dateStr])

  useEffect(() => { load() }, [load])

  async function submit() {
    const c = parseFloat(kcal)
    if (!title.trim() || !Number.isFinite(c) || c <= 0) {
      setMsg('Title and calories > 0 are required.')
      return
    }
    setBusy(true)
    setMsg(null)
    try {
      await authClient.api('/activities', {
        method: 'POST',
        body: JSON.stringify({
          title: title.trim(),
          description: description.trim() || null,
          calories_burned: c,
          activity_date: formDate,
        }),
      })
      setTitle('')
      setDescription('')
      setKcal('')
      setFormDate(dateStr)
      setShowForm(false)
      await load()
      setMsg('Activity logged.')
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    try {
      await authClient.api(`/activities/${id}`, { method: 'DELETE' })
      await load()
      setMsg('Deleted.')
    } catch (e) {
      setMsg(String(e))
    }
  }

  const isToday = dateStr === isoDate()

  return (
    <div className="activities-view">
      <div className="activities-header">
        <div>
          <h2>Activities</h2>
          <p className="page-sub">
            {date.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric', year: 'numeric' })}
          </p>
        </div>
        <div className="day-nav">
          <button className="day-nav-btn" onClick={() => setDate(shiftDate(date, -1))} aria-label="Previous day">‹</button>
          <span className="day-nav-label">{isToday ? 'Today' : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}</span>
          <button className="day-nav-btn" onClick={() => setDate(shiftDate(date, 1))} aria-label="Next day">›</button>
        </div>
      </div>

      {/* Day total banner */}
      <div className="day-total-banner">
        <span className="day-total-kcal">{Math.round(total)}</span>
        <span className="day-total-label">kcal burned · {items.length} logged</span>
      </div>

      {/* Add form accordion */}
      <div className="activities-section">
        <button
          className={`add-activity-toggle${showForm ? ' expanded' : ''}`}
          onClick={() => setShowForm((s) => !s)}
          aria-expanded={showForm}
        >
          <span className="add-icon">+</span> Add activity
          <span className={`add-chevron${showForm ? ' open' : ''}`}>▾</span>
        </button>
        {showForm && (
          <div className="add-activity-form">
            <div className="field-row">
              <div className="field">
                <label htmlFor="act-title">Title</label>
                <input id="act-title" type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Running" />
              </div>
              <div className="field">
                <label htmlFor="act-kcal">Calories</label>
                <input id="act-kcal" type="number" inputMode="decimal" value={kcal} onChange={(e) => setKcal(e.target.value)} placeholder="e.g. 350" />
              </div>
            </div>
            <div className="field">
              <label htmlFor="act-desc">Description (optional)</label>
              <input id="act-desc" type="text" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="e.g. Morning jog in the park" />
            </div>
            <div className="field">
              <label htmlFor="act-date">Date</label>
              <input id="act-date" type="date" value={formDate} onChange={(e) => setFormDate(e.target.value)} />
            </div>
            <div className="form-actions">
              <button onClick={submit} disabled={busy}>{busy ? 'Saving…' : 'Log activity'}</button>
              <button className="secondary" onClick={() => setShowForm(false)}>Cancel</button>
            </div>
            {msg && <div className="status" style={{ marginTop: 8 }}>{msg}</div>}
          </div>
        )}
      </div>

      {error && <div className="error">{error}</div>}

      {/* Activity list */}
      <div className="activities-section">
        {loading ? (
          <div className="loading">Loading…</div>
        ) : items.length === 0 ? (
          <div className="empty-state">
            <span className="empty-icon">🏃</span>
            <span>No activities logged for this day.</span>
          </div>
        ) : (
          <ul className="activity-list">
            {items.map((a) => (
              <li key={a.id} className="activity-row">
                <span className="activity-chip">🏃</span>
                <div className="activity-main">
                  <span className="activity-title">{a.title}</span>
                  {a.description && <span className="activity-desc">{a.description}</span>}
                </div>
                <span className="activity-kcal">{Math.round(a.calories_burned)} kcal</span>
                <button className="activity-del" onClick={() => remove(a.id)} aria-label={`Delete ${a.title}`}>✕</button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}