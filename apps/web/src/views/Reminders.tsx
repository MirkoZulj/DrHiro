import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'
import { Reminder, ReminderOccurrence } from '../lib/types'
import { fmtDateTime, titleCase } from '../lib/format'

const REMINDER_TYPES = ['bp', 'weight', 'meal', 'water', 'sync', 'activity', 'bedtime', 'weekly']

const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** Human-readable schedule from schedule_json, e.g. "Weekdays at 09:00". */
function fmtSchedule(schedule: Record<string, unknown>): string {
  const days = (Array.isArray(schedule.days_of_week) && (schedule.days_of_week as number[])) || []
  const time = typeof schedule.time === 'string' ? schedule.time : null
  const timePart = time ? ` at ${time}` : ''

  let dayPart: string
  if (days.length === 0) {
    dayPart = 'Daily'
  } else {
    const set = new Set(days.map((d) => Number(d)))
    const isWeekdays = [1, 2, 3, 4, 5].every((d) => set.has(d)) && !set.has(6) && !set.has(7)
    const isWeekend = set.has(6) && set.has(7) && [1, 2, 3, 4, 5].every((d) => !set.has(d))
    if (isWeekdays) dayPart = 'Weekdays'
    else if (isWeekend) dayPart = 'Weekends'
    else dayPart = [...set]
      .sort((a, b) => a - b)
      .map((d) => DAY_NAMES[(d - 1) % 7])
      .join(', ')
  }
  return dayPart + timePart
}

export default function Reminders() {
  const [reminders, setReminders] = useState<Reminder[]>([])
  const [occurrences, setOccurrences] = useState<ReminderOccurrence[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // create / edit form
  const [editingId, setEditingId] = useState<string | null>(null)
  const [type, setType] = useState('water')
  const [time, setTime] = useState('09:00')
  const [days, setDays] = useState('1,2,3,4,5')
  const [creating, setCreating] = useState(false)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const [r, o] = await Promise.all([
        authClient.api('/reminders'),
        authClient.api('/reminders/occurrences').catch(() => []),
      ])
      setReminders(Array.isArray(r) ? r : [])
      setOccurrences(Array.isArray(o) ? o : [])
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  function editReminder(r: Reminder) {
    setEditingId(r.id)
    setType(r.type)
    const sch = (r.schedule_json ?? {}) as Record<string, unknown>
    if (typeof sch.time === 'string') setTime(sch.time)
    const daysArr = Array.isArray(sch.days_of_week) ? (sch.days_of_week as number[]).join(',') : ''
    setDays(daysArr || '1,2,3,4,5')
  }

  function resetForm() {
    setEditingId(null)
    setType('water')
    setTime('09:00')
    setDays('1,2,3,4,5')
  }

  async function createReminder() {
    setCreating(true)
    setError(null)
    try {
      const schedule = days
        ? { days_of_week: days.split(',').map((d) => Number(d.trim())).filter((n) => !Number.isNaN(n)), time }
        : { time }
      if (editingId) {
        await authClient.api(`/reminders/${editingId}`, {
          method: 'PATCH',
          body: JSON.stringify({ type, schedule_json: schedule, enabled: true }),
        })
      } else {
        await authClient.api('/reminders', {
          method: 'POST',
          body: JSON.stringify({ type, schedule_json: schedule }),
        })
      }
      resetForm()
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setCreating(false)
    }
  }

  async function toggleEnabled(r: Reminder) {
    setError(null)
    try {
      await authClient.api(`/reminders/${r.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !r.enabled }),
      })
      await load()
    } catch (e) {
      setError(String(e))
    }
  }

  async function act(occId: string, action: 'snooze' | 'skip') {
    setError(null)
    try {
      await authClient.api(`/reminders/occurrences/${occId}/${action}`, { method: 'POST' })
      await load()
    } catch (e) {
      setError(String(e))
    }
  }

  const upcoming = occurrences.filter((o) => o.status !== 'done' && o.status !== 'skipped' && o.status !== 'completed')

  return (
    <div className="reminders">
      <h2>Reminders</h2>
      <p className="page-sub">Stay on top of your health routine.</p>

      {error && <div className="error">{error}</div>}

      <section className="section">
        <h3>{editingId ? 'Edit reminder' : 'Create reminder'}</h3>
        <div className="form-card">
          <div className="field-row">
            <div className="field">
              <label>Type</label>
              <select value={type} onChange={(e) => setType(e.target.value)}>
                {REMINDER_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {titleCase(t)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Time</label>
              <input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
            </div>
          </div>
          <div className="field">
            <label>Days of week (1=Mon … 7=Sun, comma separated)</label>
            <input value={days} onChange={(e) => setDays(e.target.value)} placeholder="1,2,3,4,5" />
          </div>
          <div className="form-actions">
            <button onClick={createReminder} disabled={creating || !time}>
              {creating ? 'Saving…' : editingId ? 'Save changes' : 'Add reminder'}
            </button>
            {editingId && (
              <button className="small secondary" onClick={resetForm}>
                Cancel edit
              </button>
            )}
          </div>
        </div>
      </section>

      <section className="section">
        <h3>Your reminders</h3>
        {loading ? (
          <div className="loading">Loading…</div>
        ) : reminders.length === 0 ? (
          <div className="empty">No reminders set.</div>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th scope="col">Type</th>
                  <th scope="col">Schedule</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">
                    Occurrences
                  </th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {reminders.map((r) => {
                  const occCount = occurrences.filter((o) => o.reminder_id === r.id).length
                  return (
                    <tr key={r.id}>
                      <td className="strong">{titleCase(r.type)}</td>
                      <td>{fmtSchedule(r.schedule_json)}</td>
                      <td>
                        <span className={`badge ${r.enabled ? 'on' : 'off'}`}>{r.enabled ? 'On' : 'Off'}</span>
                      </td>
                      <td className="num">{occCount}</td>
                      <td>
                        <div className="occ-actions">
                          <button className="small secondary" onClick={() => editReminder(r)}>
                            Edit
                          </button>
                          <button className="small secondary" onClick={() => toggleEnabled(r)}>
                            {r.enabled ? 'Turn off' : 'Turn on'}
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="section">
        <h3>Upcoming occurrences</h3>
        {loading ? (
          <div className="loading">Loading…</div>
        ) : upcoming.length === 0 ? (
          <div className="empty">No upcoming occurrences.</div>
        ) : (
          <ul>
            {upcoming.map((o) => (
              <li key={o.id} className="list-item">
                <div className="main">
                  <div className="title">{titleCase(o.reminder_id || 'Reminder')}</div>
                  {o.due_at && <div className="desc">{fmtDateTime(o.due_at)}</div>}
                </div>
                <div className="occ-actions">
                  <button className="small secondary" onClick={() => act(o.id, 'snooze')}>
                    Snooze
                  </button>
                  <button className="small secondary" onClick={() => act(o.id, 'skip')}>
                    Skip
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
