import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { authClient } from '../lib/auth'

interface MedItem {
  id: string
  kind: string
  name: string
  brand?: string | null
  ingredients_json?: Array<Record<string, unknown>> | null
  dose_amount?: number | null
  dose_unit?: string | null
  schedule_json?: Record<string, unknown> | null
  start_date?: string | null
  status: string
  stock_quantity?: number | null
  notes?: string | null
}

interface AdherenceBucket {
  start: string
  end: string
  expected: number
  taken: number
  skipped: number
  not_logged: number
  pct_taken: number | null
}

function fmtSchedule(s: Record<string, unknown> | null | undefined): string {
  if (!s) return '—'
  const t = String(s.type || '')
  const times = Array.isArray(s.times) ? (s.times as string[]).join(', ') : ''
  if (t === 'daily') return `Daily at ${times || '—'}`
  if (t === 'weekly') return `Weekly (${(s.days as string[] | undefined)?.join(',') || '—'}) at ${times}`
  if (t === 'interval') return `Every ${s.every_days} days at ${times}`
  if (t === 'prn') return 'As needed (PRN)'
  return t || '—'
}


/** Wave 3 (spec §5.8): Meds dashboard — active list, today strip, adherence
 *  day/week/month view, history timeline. */
export default function Medications() {
  const [items, setItems] = useState<MedItem[]>([])
  const [adherence, setAdherence] = useState<AdherenceBucket[]>([])
  const [granularity, setGranularity] = useState<'day' | 'week' | 'month'>('day')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [tab, setTab] = useState<'supplement' | 'medication'>('supplement')
  const [detail, setDetail] = useState<any | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  // add form
  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')
  const [kind, setKind] = useState('supplement')
  const [dose, setDose] = useState('1')
  const [doseUnit, setDoseUnit] = useState('tablet')
  const [time, setTime] = useState('08:00')

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const [itemsRes, adhRes] = await Promise.all([
        authClient.api('/medications?status=all'),
        authClient.api(`/medications/adherence?granularity=${granularity}&offset=0`).catch(() => ({ buckets: [] })),
      ])
      setItems(itemsRes.items || [])
      setAdherence(adhRes.buckets || [])
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [granularity])

  async function openSupplement(item: MedItem) {
    if (expandedId === item.id) { setExpandedId(null); setDetail(null); return }
    setExpandedId(item.id)
    setDetailLoading(true)
    try {
      const res = await authClient.api(`/medications/${item.id}/composition`)
      setDetail(res)
    } catch (e) {
      setMsg(String(e))
    } finally {
      setDetailLoading(false)
    }
  }

  async function logDose(item: MedItem, status: 'taken' | 'skipped') {
    setBusyId(item.id)
    setMsg(null)
    try {
      await authClient.api(`/medications/${item.id}/doses`, {
        method: 'POST',
        body: JSON.stringify({ status }),
      })
      setMsg(`Logged ${item.name} — ${status}`)
      await load()
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusyId(null)
    }
  }

  async function setStatus(item: MedItem, action: 'pause' | 'resume' | 'stop') {
    setBusyId(item.id)
    try {
      await authClient.api(`/medications/${item.id}/${action}`, { method: 'POST', body: JSON.stringify({}) })
      await load()
    } catch (e) {
      setMsg(String(e))
    } finally {
      setBusyId(null)
    }
  }

  async function addItem() {
    setMsg(null)
    try {
      await authClient.api('/medications', {
        method: 'POST',
        body: JSON.stringify({
          kind,
          name,
          dose_amount: Number(dose) || null,
          dose_unit: doseUnit,
          schedule_json: { type: 'daily', times: [time] },
        }),
      })
      setAdding(false)
      setName('')
      await load()
    } catch (e) {
      setMsg(String(e))
    }
  }

  const active = items.filter((i) => i.status === 'active')
  const supplements = active.filter((i) => i.kind === 'supplement')
  const medications = active.filter((i) => i.kind !== 'supplement')
  const shown = tab === 'supplement' ? supplements : medications

  function itemCard(i: MedItem) {
    const expanded = expandedId === i.id
    return (
      <div className="card" key={i.id}>
        <div className="label">
          {i.kind === 'medication' ? '💊' : '🌿'} {i.name}
          {i.brand ? ` · ${i.brand}` : ''}
        </div>
        <div className="value">
          {i.dose_amount ?? '—'} <small>{i.dose_unit || ''}</small>
        </div>
        <div className="sub">{fmtSchedule(i.schedule_json)}</div>
        <div className="sub">
          since {i.start_date || '—'}
          {i.stock_quantity !== null && i.stock_quantity !== undefined ? ` · stock: ${i.stock_quantity}` : ''}
        </div>
        <div className="form-actions" style={{ marginTop: 8 }}>
          <button className="secondary" onClick={() => openSupplement(i)}>
            {expanded ? 'Hide detail' : 'Detail'}
          </button>
          <button disabled={busyId === i.id} onClick={() => logDose(i, 'taken')}>✓ Taken</button>
          <button className="secondary" disabled={busyId === i.id} onClick={() => logDose(i, 'skipped')}>– Skip</button>
          <button className="secondary" disabled={busyId === i.id} onClick={() => setStatus(i, 'pause')}>Pause</button>
          <button className="danger" disabled={busyId === i.id} onClick={() => setStatus(i, 'stop')}>Stop</button>
        </div>
        {expanded && (
          <div style={{ marginTop: 8 }}>
            {detailLoading && detail?.item_id !== i.id ? (
              <div className="loading">Loading…</div>
            ) : detail && detail.item_id === i.id ? (
              detail.rows.length === 0 ? (
                <p className="muted">No composition recorded for this item.</p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Component</th>
                      <th>Per capsule</th>
                      {detail.dose_amount > 1 && <th>Per dose ({detail.dose_amount} ×)</th>}
                      <th>Daily required</th>
                      <th>% per capsule</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.rows.map((r: any, idx: number) => (
                      <tr key={idx}>
                        <td>{r.component}</td>
                        <td>{r.per_capsule === null ? '—' : `${r.per_capsule} ${r.unit}`}</td>
                        {detail.dose_amount > 1 && (
                          <td>{r.per_dose_total === null ? '—' : `${r.per_dose_total} ${r.unit}`}</td>
                        )}
                        <td>{r.rda === null ? '—' : `${r.rda} ${r.unit}`}</td>
                        <td>{r.pct_per_capsule === null ? '—' : `${r.pct_per_capsule}%`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )
            ) : (
              <p className="muted">No composition recorded for this item.</p>
            )}
          </div>
        )}
      </div>
    )
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: '8px 0' }}>Meds &amp; Supplements</h2>
        <button className="secondary" onClick={() => setAdding(!adding)}>
          {adding ? 'Cancel' : '+ Add'}
        </button>
      </div>

      {msg && <div className="status">{msg}</div>}
      {error && <div className="status">Error: {error}</div>}

      {adding && (
        <section className="section">
          <div className="form-card">
            <div style={{ display: 'grid', gap: 8, gridTemplateColumns: '1fr 1fr' }}>
              <label>
                Kind
                <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: '100%' }}>
                  <option value="supplement">Supplement</option>
                  <option value="medication">Medication</option>
                </select>
              </label>
              <label>
                Name
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Vitamin D3" style={{ width: '100%' }} />
              </label>
              <label>
                Dose
                <input value={dose} onChange={(e) => setDose(e.target.value)} style={{ width: '100%' }} />
              </label>
              <label>
                Unit
                <input value={doseUnit} onChange={(e) => setDoseUnit(e.target.value)} placeholder="tablet" style={{ width: '100%' }} />
              </label>
              <label>
                Time
                <input value={time} onChange={(e) => setTime(e.target.value)} placeholder="08:00" style={{ width: '100%' }} />
              </label>
            </div>
            <div className="form-actions" style={{ marginTop: 12 }}>
              <button onClick={addItem} disabled={!name}>Save</button>
            </div>
          </div>
        </section>
      )}

      {loading ? (
        <div className="loading">Loading…</div>
      ) : (
        <>
          <section className="section">
            <h3>Active ({active.length})</h3>
            {active.length === 0 && <p className="muted">Nothing active. Add a supplement or medication above.</p>}
            <div className="form-actions" style={{ marginBottom: 8 }}>
              <button className={tab === 'supplement' ? '' : 'secondary'} onClick={() => setTab('supplement')}>
                Supplements ({supplements.length})
              </button>
              <button className={tab === 'medication' ? '' : 'secondary'} onClick={() => setTab('medication')}>
                Meds ({medications.length})
              </button>
            </div>
            {shown.length === 0 && (
              <p className="muted">
                {tab === 'supplement' ? 'No active supplements.' : 'No active medications.'}
              </p>
            )}
            <div className="cards">{shown.map(itemCard)}</div>
          </section>

          <section className="section">
            <h3>Adherence</h3>
            <div className="form-actions" style={{ marginBottom: 8 }}>
              {(['day', 'week', 'month'] as const).map((g) => (
                <button key={g} className={granularity === g ? '' : 'secondary'} onClick={() => setGranularity(g)}>
                  {g === 'day' ? 'Day' : g === 'week' ? 'Week' : 'Month'}
                </button>
              ))}
            </div>
            <table>
              <thead>
                <tr>
                  <th>Period</th>
                  <th>Expected</th>
                  <th>Taken</th>
                  <th>Skipped</th>
                  <th>Not logged</th>
                  <th>%</th>
                </tr>
              </thead>
              <tbody>
                {adherence.length === 0 && (
                  <tr><td colSpan={6} className="muted">No adherence data yet.</td></tr>
                )}
                {adherence.map((b, idx) => (
                  <tr key={idx}>
                    <td>{b.start}</td>
                    <td>{b.expected}</td>
                    <td>{b.taken}</td>
                    <td>{b.skipped}</td>
                    <td>{b.not_logged}</td>
                    <td>{b.pct_taken === null ? <em className="muted">not logged</em> : `${Math.round(b.pct_taken)}%`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted" style={{ fontSize: 12 }}>
              A dose with no log is “not logged” — never counted as missed or 0%.
            </p>
          </section>

          <section className="section">
            <h3>Paused / stopped</h3>
            {items.filter((i) => i.status !== 'active').length === 0 && <p className="muted">None.</p>}
            <ul>
              {items
                .filter((i) => i.status !== 'active')
                .map((i) => (
                  <li key={i.id}>
                    {i.name} — {i.status}{' '}
                    {i.status === 'paused' && (
                      <button className="secondary" style={{ marginLeft: 8 }} onClick={() => setStatus(i, 'resume')}>
                        Resume
                      </button>
                    )}
                  </li>
                ))}
            </ul>
          </section>

          <p className="muted" style={{ fontSize: 12 }}>
            Questions about whether to take something belong with your doctor — add them to a{' '}
            <Link to="/analytics">visit summary</Link>.
          </p>
        </>
      )}
    </div>
  )
}
