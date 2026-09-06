import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'
import { fmtDate } from '../lib/format'

/* ===================== ACTIVITY POPUP ===================== */

interface ActivityItem {
  id: string
  date: string
  title: string
  description: string | null
  calories_burned: number
}

const QUICK_LOG = ['Running', 'Weightlifting', 'Crossfit', 'Pilates', 'Yoga', 'Swimming']

/* Katch–McArdle: BMR = 370 + 21.6 × LBM(kg); LBM = weight × (1 − bodyfat/100) */
function katchMcardle(weightKg: number, bodyFatPct: number): number {
  const lbm = weightKg * (1 - bodyFatPct / 100)
  return 370 + 21.6 * lbm
}

export function ActivityPopup({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [, setBmr] = useState<number | null>(null)
  const [bmrDraft, setBmrDraft] = useState('')
  const [calcWeight, setCalcWeight] = useState('')
  const [calcFat, setCalcFat] = useState('')
  const [calcResult, setCalcResult] = useState<number | null>(null)
  const [items, setItems] = useState<ActivityItem[]>([])
  const [todayTotal, setTodayTotal] = useState(0)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [kcal, setKcal] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  useEffect(() => {
    authClient.api('/activity/settings').then((s) => {
      setBmr(s.basal_metabolism_kcal)
      if (s.basal_metabolism_kcal) setBmrDraft(String(s.basal_metabolism_kcal))
    }).catch(() => {})
    loadActivities()
  }, [])

  function loadActivities() {
    const today = new Date().toISOString().slice(0, 10)
    Promise.all([
      authClient.api(`/activities?date=${today}`),
      authClient.api('/activities'),
    ]).then(([todayRes]) => {
      setItems(todayRes.items ?? [])
      setTodayTotal(todayRes.total_burned ?? 0)
    }).catch(() => {})
  }

  async function saveBmr(v: number) {
    setMsg(null)
    try {
      await authClient.api('/activity/settings', { method: 'PATCH', body: JSON.stringify({ basal_metabolism_kcal: v }) })
      setBmr(v)
      setMsg('BMR saved — applies to all days.')
      onSaved()
    } catch (e) { setMsg(String(e)) }
  }

  function applyCalculator() {
    const w = parseFloat(calcWeight); const f = parseFloat(calcFat)
    if (!Number.isFinite(w) || !Number.isFinite(f) || w <= 0 || f < 0 || f >= 60) {
      setMsg('Enter valid weight (kg) and body fat (%).')
      return
    }
    const bmrCalc = Math.round(katchMcardle(w, f))
    setCalcResult(bmrCalc)
    setBmrDraft(String(bmrCalc))
    setMsg(null)
  }

  async function logActivity() {
    const c = parseFloat(kcal)
    if (!title.trim() || !Number.isFinite(c) || c <= 0) {
      setMsg('Title and calories > 0 are required.')
      return
    }
    setBusy(true); setMsg(null)
    try {
      await authClient.api('/activities', {
        method: 'POST',
        body: JSON.stringify({ title: title.trim(), description: description.trim() || null, calories_burned: c }),
      })
      setTitle(''); setDescription(''); setKcal('')
      loadActivities()
      onSaved()
    } catch (e) { setMsg(String(e)) } finally { setBusy(false) }
  }

  async function removeActivity(id: string) {
    try {
      await authClient.api(`/activities/${id}`, { method: 'DELETE' })
      loadActivities(); onSaved()
    } catch (e) { setMsg(String(e)) }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Activity details">
        <div className="modal-header">
          <div className="modal-title-row">
            <span className="modal-icon" style={{ color: 'var(--metric-color)' }}>🏃</span>
            <h2>Activity</h2>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="modal-content">
          {/* Today's burned total */}
          <section className="section">
            <div className="balance-hero">
              <span className="balance-number">{Math.round(todayTotal)}</span>
              <span className="balance-unit">kcal burned today (sport)</span>
            </div>
          </section>

          {/* BMR */}
          <section className="section">
            <h3>Basal metabolism (BMR)</h3>
            <p className="popup-hint">Applies to every day. Use the calculator or enter a known value.</p>
            <div className="quick-entry-row">
              <input
                type="number"
                inputMode="decimal"
                value={bmrDraft}
                onChange={(e) => setBmrDraft(e.target.value)}
                placeholder="e.g. 1680"
              />
              <button className="small" disabled={busy || !bmrDraft}
                onClick={() => { const v = parseFloat(bmrDraft); if (Number.isFinite(v)) saveBmr(v) }}>
                Save
              </button>
            </div>
            {/* Katch–McArdle calculator */}
            <div className="bmr-calc">
              <div className="quick-entry-row">
                <input type="number" inputMode="decimal" value={calcWeight} onChange={(e) => setCalcWeight(e.target.value)} placeholder="Weight (kg)" />
                <input type="number" inputMode="decimal" value={calcFat} onChange={(e) => setCalcFat(e.target.value)} placeholder="Body fat (%)" />
                <button className="small" onClick={applyCalculator}>Calc</button>
              </div>
              {calcResult != null && (
                <p className="popup-hint">Katch–McArdle result: <b>{calcResult} kcal/day</b> — press Save above to apply.¹</p>
              )}
              <p className="footnote">¹ BMR = 370 + 21.6 × LBM; LBM = weight × (1 − body fat %/100). Katch–McArdle formula.</p>
            </div>
          </section>

          {/* Quick log */}
          <section className="section">
            <h3>Quick log</h3>
            <div className="quicklog-row">
              {QUICK_LOG.map((q) => (
                <button key={q} className={`quicklog-btn${title === q ? ' active' : ''}`}
                  onClick={() => setTitle(q)}>{q}</button>
              ))}
            </div>
          </section>

          {/* Free entry */}
          <section className="section">
            <h3>Log activity</h3>
            <div className="quick-entry-row">
              <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title (e.g. Running)" />
            </div>
            <div className="quick-entry-row">
              <input type="text" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Description (optional)" />
            </div>
            <div className="quick-entry-row">
              <input type="number" inputMode="decimal" value={kcal} onChange={(e) => setKcal(e.target.value)} placeholder="Calories spent (kcal)" />
              <button className="small" onClick={logActivity} disabled={busy}>{busy ? '…' : 'Log'}</button>
            </div>
            {msg && <p className="popup-hint">{msg}</p>}
          </section>

          {/* Today's activities */}
          <section className="section">
            <h3>Today's activities</h3>
            {items.length === 0 ? (
              <div className="empty">Nothing logged today.</div>
            ) : (
              <ul className="activity-list">
                {items.map((a) => (
                  <li key={a.id} className="activity-item">
                    <div className="activity-main">
                      <span className="activity-title">{a.title}</span>
                      {a.description && <span className="activity-desc">{a.description}</span>}
                    </div>
                    <span className="activity-kcal">{Math.round(a.calories_burned)} kcal</span>
                    <button className="activity-del" onClick={() => removeActivity(a.id)} aria-label="Delete">✕</button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

/* ===================== BALANCE POPUP ===================== */

interface BalancePoint {
  date: string
  intake_kcal: number
  burned_kcal: number
  bmr_kcal: number
  walking_kcal: number
  sport_kcal: number
  balance_kcal: number
}

export function BalancePopup({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [points, setPoints] = useState<BalancePoint[]>([])
  const [bmr, setBmr] = useState(0)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    authClient.api('/energy-balance?days=30').then((r) => {
      setPoints(r.points ?? [])
      setBmr(r.bmr_kcal ?? 0)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  const todayPt = points[points.length - 1]
  const maxAbs = Math.max(...points.map((p) => Math.abs(p.balance_kcal)), 1)

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="Calorie balance details">
        <div className="modal-header">
          <div className="modal-title-row">
            <span className="modal-icon" style={{ color: 'var(--metric-color)' }}>⚖️</span>
            <h2>Calorie balance</h2>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="modal-content">
          {loading ? (
            <div className="loading">Loading…</div>
          ) : (
            <>
              {todayPt && (
                <section className="section">
                  <div className="balance-hero">
                    <span className={`balance-number ${todayPt.balance_kcal >= 0 ? 'pos' : 'neg'}`}>
                      {todayPt.balance_kcal > 0 ? '+' : ''}{Math.round(todayPt.balance_kcal)}
                    </span>
                    <span className="balance-unit">kcal today (intake {Math.round(todayPt.intake_kcal)} − burned {Math.round(todayPt.burned_kcal)})</span>
                  </div>
                  <p className="popup-hint">Burned = BMR {Math.round(todayPt.bmr_kcal)} + walking {Math.round(todayPt.walking_kcal)} + sport {Math.round(todayPt.sport_kcal)} kcal.</p>
                </section>
              )}

              <section className="section">
                <h3>Daily balance — last 30 days</h3>
                <div className="card">
                  <div className="bars bars-balance">
                    {points.map((p) => (
                      <div key={p.date} className="balance-bar-wrap" title={`${fmtDate(p.date)}: ${p.balance_kcal > 0 ? '+' : ''}${Math.round(p.balance_kcal)} kcal`}>
                        <div className="balance-bar-stack">
                          {p.balance_kcal > 0 && <div className="balance-bar pos" style={{ height: `${(Math.abs(p.balance_kcal) / maxAbs) * 50}%` }} />}
                          <div className="balance-bar-zero" />
                          {p.balance_kcal < 0 && <div className="balance-bar neg" style={{ height: `${(Math.abs(p.balance_kcal) / maxAbs) * 50}%` }} />}
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="bar-axis">
                    {points.filter((_, i) => i % 5 === 0).map((p) => (<span key={p.date}>{fmtDate(p.date)}</span>))}
                  </div>
                  <p className="popup-hint">Green: surplus (intake &gt; burned). Red: deficit.</p>
                </div>
              </section>

              {bmr === 0 && (
                <p className="note warn">BMR not set yet — open the Activity tile and enter it for accurate balance.</p>
              )}
              <button className="small balance-refresh" onClick={onSaved}>Refresh</button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
