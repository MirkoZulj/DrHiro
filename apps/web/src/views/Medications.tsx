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

interface CompositionRow {
  component: string
  nutrient_key?: string | null
  per_capsule: number | null
  unit?: string | null
  per_dose_total?: number | null
  rda: number | null
  pct_per_capsule: number | null
}

interface Composition {
  item_id: string
  name: string
  kind: string
  dose_amount?: number | null
  dose_unit?: string | null
  rows: CompositionRow[]
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

function nextReminder(s: Record<string, unknown> | null | undefined): string {
  if (!s) return '—'
  const times = Array.isArray(s.times) ? (s.times as string[]) : []
  if (!times.length) return fmtSchedule(s)
  return `Next: ${times[0]}`
}

/** Trim trailing zeros: 10.00 -> "10", 2.40 -> "2.4", 0.50 -> "0.5". */
function trimNum(n: number, maxDec: number): string {
  const fixed = n.toFixed(maxDec)
  return fixed.includes('.') ? fixed.replace(/0+$/, '').replace(/\.$/, '') : fixed
}

/**
 * Format an amount with its unit, keeping small values in a readable unit.
 * Canonical API units are mcg/mg, but defensively: <1 mg is shown as mcg
 * (so B12 never appears as "0.01 mg"), and <1 g as mg.
 */
function fmtAmount(value: number | null | undefined, unit: string | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  let u = (unit || '').replace('µg', 'mcg').replace('ug', 'mcg').trim()
  let v = value
  if (u === 'g' && Math.abs(v) < 1) { v *= 1000; u = 'mg' }
  if (u === 'mg' && v !== 0 && Math.abs(v) < 1) { v *= 1000; u = 'mcg' }
  const dec = Math.abs(v) >= 100 ? 0 : Math.abs(v) >= 10 ? 1 : 2
  const num = Math.abs(v) >= 1000
    ? Math.round(v).toLocaleString('en-US')
    : trimNum(v, dec)
  return u ? `${num} ${u}` : num
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${trimNum(v, 1)}%`
}

function statusLabel(status: string): string {
  return status.charAt(0).toUpperCase() + status.slice(1)
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
  const [tab, setTab] = useState<'supplement' | 'medication'>('supplement')
  const [detailItem, setDetailItem] = useState<MedItem | null>(null)
  const [detail, setDetail] = useState<Composition | null>(null)
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

  async function openDetail(item: MedItem) {
    setDetailItem(item)
    setDetail(null)
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

  function closeDetail() {
    setDetailItem(null)
    setDetail(null)
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
  const inactive = items.filter((i) => i.status !== 'active')

  function itemRow(i: MedItem) {
    const busy = busyId === i.id
    return (
      <article className="meds-item" key={i.id}>
        <div className="meds-item-main">
          <span className="meds-item-icon" aria-hidden="true">
            {i.kind === 'medication' ? '💊' : '🌿'}
          </span>
          <div className="meds-item-body">
            <div className="meds-item-title">
              {i.name}
              {i.brand ? <span className="meds-item-brand"> · {i.brand}</span> : null}
            </div>
            <div className="meds-item-meta">
              <span>{i.dose_amount ?? '—'}{i.dose_unit ? ` ${i.dose_unit}` : ''}</span>
              <span className="meds-dot" aria-hidden="true">·</span>
              <span>{fmtSchedule(i.schedule_json)}</span>
            </div>
            <div className="meds-item-sub">
              <span>{nextReminder(i.schedule_json)}</span>
              <span className="meds-dot" aria-hidden="true">·</span>
              <span>since {i.start_date || '—'}</span>
              {i.stock_quantity !== null && i.stock_quantity !== undefined ? (
                <><span className="meds-dot" aria-hidden="true">·</span><span>stock: {i.stock_quantity}</span></>
              ) : null}
            </div>
          </div>
          <span className={`meds-badge meds-badge--${i.status}`}>{statusLabel(i.status)}</span>
        </div>
        {/* ONE consistent action row per item — not duplicated anywhere else */}
        <div className="meds-actions">
          <button disabled={busy} onClick={() => logDose(i, 'taken')}>✓ Taken</button>
          <button className="secondary" disabled={busy} onClick={() => logDose(i, 'skipped')}>Skip</button>
          <button className="secondary" disabled={busy} onClick={() => setStatus(i, 'pause')}>Pause</button>
          <button className="danger" disabled={busy} onClick={() => setStatus(i, 'stop')}>Stop</button>
          <button className="link meds-detail-btn" onClick={() => openDetail(i)}>Detail</button>
        </div>
      </article>
    )
  }

  const showPerDose = !!detail && (detail.dose_amount ?? 1) > 1

  return (
    <div className="meds-view">
      <div className="meds-header">
        <h2>Meds &amp; Supplements</h2>
        <button className="secondary" onClick={() => setAdding(!adding)}>
          {adding ? 'Cancel' : '+ Add'}
        </button>
      </div>

      {msg && <div className="status">{msg}</div>}
      {error && <div className="status">Error: {error}</div>}

      {adding && (
        <section className="section">
          <div className="form-card">
            <div className="meds-add-grid">
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
            <div className="meds-tabs-row">
              <div className="tabs meds-tabs" role="tablist" aria-label="Kind">
                <button
                  className={`tab ${tab === 'supplement' ? 'active' : ''}`}
                  role="tab"
                  aria-selected={tab === 'supplement'}
                  onClick={() => setTab('supplement')}
                >
                  Supplements ({supplements.length})
                </button>
                <button
                  className={`tab ${tab === 'medication' ? 'active' : ''}`}
                  role="tab"
                  aria-selected={tab === 'medication'}
                  onClick={() => setTab('medication')}
                >
                  Meds ({medications.length})
                </button>
              </div>
              <span className="meds-count">{active.length} active</span>
            </div>

            {shown.length === 0 ? (
              <div className="empty">
                {tab === 'supplement' ? 'No active supplements.' : 'No active medications.'}
              </div>
            ) : (
              <div className="meds-list">{shown.map(itemRow)}</div>
            )}
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
            <div className="table-wrap">
              <table className="data-table meds-adherence">
                <thead>
                  <tr>
                    <th>Period</th>
                    <th className="num">Expected</th>
                    <th className="num">Taken</th>
                    <th className="num">Skipped</th>
                    <th className="num">Not logged</th>
                    <th className="num">%</th>
                  </tr>
                </thead>
                <tbody>
                  {adherence.length === 0 && (
                    <tr><td colSpan={6} className="muted" style={{ textAlign: 'center' }}>No adherence data yet.</td></tr>
                  )}
                  {adherence.map((b, idx) => (
                    <tr key={idx}>
                      <td>{b.start}</td>
                      <td className="num">{b.expected}</td>
                      <td className="num">{b.taken}</td>
                      <td className="num">{b.skipped}</td>
                      <td className="num">{b.not_logged}</td>
                      <td className="num">{b.pct_taken === null ? <em className="muted">not logged</em> : `${Math.round(b.pct_taken)}%`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted" style={{ fontSize: 12 }}>
              A dose with no log is “not logged” — never counted as missed or 0%.
            </p>
          </section>

          <section className="section">
            <h3>Paused / stopped</h3>
            {inactive.length === 0 ? (
              <p className="muted">None.</p>
            ) : (
              <div className="meds-list">
                {inactive.map((i) => (
                  <article className="meds-item meds-item--inactive" key={i.id}>
                    <div className="meds-item-main">
                      <span className="meds-item-icon" aria-hidden="true">
                        {i.kind === 'medication' ? '💊' : '🌿'}
                      </span>
                      <div className="meds-item-body">
                        <div className="meds-item-title">{i.name}{i.brand ? <span className="meds-item-brand"> · {i.brand}</span> : null}</div>
                        <div className="meds-item-meta">
                          <span>{i.dose_amount ?? '—'}{i.dose_unit ? ` ${i.dose_unit}` : ''}</span>
                          <span className="meds-dot" aria-hidden="true">·</span>
                          <span>{fmtSchedule(i.schedule_json)}</span>
                        </div>
                      </div>
                      <span className={`meds-badge meds-badge--${i.status}`}>{statusLabel(i.status)}</span>
                    </div>
                    {i.status === 'paused' && (
                      <div className="meds-actions">
                        <button disabled={busyId === i.id} onClick={() => setStatus(i, 'resume')}>Resume</button>
                        <button className="danger" disabled={busyId === i.id} onClick={() => setStatus(i, 'stop')}>Stop</button>
                      </div>
                    )}
                  </article>
                ))}
              </div>
            )}
          </section>

          <p className="muted" style={{ fontSize: 12 }}>
            Questions about whether to take something belong with your doctor — add them to a{' '}
            <Link to="/analytics">visit summary</Link>.
          </p>
        </>
      )}

      {detailItem && (
        <div className="modal-overlay" onClick={closeDetail}>
          <div
            className="modal-popup meds-detail"
            role="dialog"
            aria-modal="true"
            aria-label={`${detailItem.name} composition`}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <div className="modal-title-row">
                <span className="modal-icon" aria-hidden="true">{detailItem.kind === 'medication' ? '💊' : '🌿'}</span>
                <h2>{detailItem.name}</h2>
              </div>
              <button className="modal-close" onClick={closeDetail} aria-label="Close">✕</button>
            </div>
            <p className="meds-detail-sub">
              {detailItem.dose_amount ?? '—'}{detailItem.dose_unit ? ` ${detailItem.dose_unit}` : ''}
              {' · '}{fmtSchedule(detailItem.schedule_json)}
              {' · '}{nextReminder(detailItem.schedule_json)}
            </p>

            {detailLoading ? (
              <div className="loading">Loading…</div>
            ) : detail && detail.rows.length > 0 ? (
              <div className="table-wrap meds-comp-wrap">
                <table className="data-table meds-comp">
                  <thead>
                    <tr>
                      <th>Component</th>
                      <th className="num">Per capsule</th>
                      {showPerDose && <th className="num">Per dose ({detail.dose_amount} ×)</th>}
                      <th className="num">Daily required</th>
                      <th className="num">% per capsule</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.rows.map((r, idx) => (
                      <tr key={idx}>
                        <td className="meds-comp-name">{r.component}</td>
                        <td className="num tabular">{fmtAmount(r.per_capsule, r.unit)}</td>
                        {showPerDose && <td className="num tabular">{fmtAmount(r.per_dose_total, r.unit)}</td>}
                        <td className="num tabular">{fmtAmount(r.rda, r.unit)}</td>
                        <td className="num tabular">{fmtPct(r.pct_per_capsule)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="muted">No composition recorded for this item.</p>
            )}
            <p className="meds-detail-note">“—” means no daily requirement is recorded for that component (e.g. glucosamine, boswellia) — not zero.</p>
          </div>
        </div>
      )}
    </div>
  )
}
