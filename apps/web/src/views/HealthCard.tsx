import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { authClient } from '../lib/auth'

const LIST_SECTIONS = [
  'conditions', 'allergies', 'intolerances', 'implants_devices', 'vaccinations',
  'surgeries', 'family_history', 'emergency_contacts', 'care_team',
] as const

const SECTION_LABELS: Record<string, string> = {
  conditions: 'Conditions',
  allergies: 'Allergies',
  intolerances: 'Intolerances',
  implants_devices: 'Implants & devices',
  vaccinations: 'Vaccinations',
  surgeries: 'Surgeries',
  family_history: 'Family history',
  emergency_contacts: 'Emergency contacts',
  care_team: 'Care team',
}

/** Entry field required per section (the "name" of the entry). */
const NAME_FIELD: Record<string, string> = {
  conditions: 'name',
  allergies: 'substance',
  intolerances: 'substance',
  implants_devices: 'name',
  vaccinations: 'vaccine',
  surgeries: 'name',
  family_history: 'condition',
  emergency_contacts: 'name',
  care_team: 'role',
}

interface Entry {
  id: string
  [k: string]: unknown
}

/** Wave 3 (spec §6.5): editable health card. Dashboard-only — no public share
 *  links in v1. Reachable from Settings and the Records page header. */
export default function HealthCard() {
  const [card, setCard] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState<string | null>(null)
  const [addingSection, setAddingSection] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  async function load() {
    setLoading(true)
    try {
      const d = await authClient.api('/health-card')
      setCard(d.card)
    } catch (e) {
      setMsg(String(e))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    load()
  }, [])

  async function addEntry(section: string) {
    try {
      const body: any = { [NAME_FIELD[section]]: draft }
      if (section === 'conditions') body.status = 'active'
      if (section === 'allergies') body.severity = 'unknown'
      await authClient.api(`/health-card/${section}`, { method: 'POST', body: JSON.stringify(body) })
      setDraft('')
      setAddingSection(null)
      await load()
    } catch (e) {
      setMsg(String(e))
    }
  }

  async function removeEntry(section: string, id: string) {
    try {
      await authClient.api(`/health-card/${section}/${id}`, { method: 'DELETE' })
      await load()
    } catch (e) {
      setMsg(String(e))
    }
  }

  async function setField(field: string, value: string) {
    try {
      await authClient.api('/health-card', {
        method: 'PUT',
        body: JSON.stringify({ [field]: value || null }),
      })
      await load()
    } catch (e) {
      setMsg(String(e))
    }
  }

  async function markReviewed() {
    try {
      await authClient.api('/health-card/reviewed', { method: 'POST' })
      await load()
      setMsg('Marked as reviewed.')
    } catch (e) {
      setMsg(String(e))
    }
  }

  async function requestPdf() {
    try {
      const d = await authClient.api('/health-card/pdf', { method: 'POST', body: JSON.stringify({}) })
      setMsg(`PDF queued (${d.file.status}) — download it from Analytics → Data & backup when ready.`)
    } catch (e) {
      setMsg(String(e))
    }
  }

  if (loading) return <div className="loading">Loading…</div>
  if (!card) return <div className="status">Error: {msg}</div>
  const s = card.stored
  const d = card.derived

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h2 style={{ margin: '8px 0' }}>Health card</h2>
        <div className="form-actions">
          <button className="secondary" onClick={requestPdf}>Download PDF</button>
          <button className="secondary" onClick={markReviewed}>Mark as reviewed</button>
        </div>
      </div>
      {msg && <div className="status">{msg}</div>}
      {card.stale_sections.length > 0 && (
        <p className="muted" style={{ fontSize: 12 }}>
          ⚠️ Not reviewed in over 12 months — please check the details below and press “Mark as reviewed”.
        </p>
      )}

      <section className="section">
        <div className="form-card">
          <div className="cards">
            <div className="card"><div className="label">Name</div><div className="value" style={{ fontSize: 18 }}>{d.display_name}</div></div>
            <div className="card"><div className="label">DOB / age</div><div className="value" style={{ fontSize: 18 }}>{d.date_of_birth || '—'} <small>{d.age ? `(${d.age})` : ''}</small></div></div>
            <div className="card"><div className="label">Sex</div><div className="value" style={{ fontSize: 18 }}>{d.sex || '—'}</div></div>
            <div className="card"><div className="label">Height</div><div className="value" style={{ fontSize: 18 }}>{d.height_cm ? `${d.height_cm} cm` : '—'}</div></div>
            <div className="card"><div className="label">Latest weight</div><div className="value" style={{ fontSize: 18 }}>{d.latest_weight_kg ? `${d.latest_weight_kg} kg` : '—'}</div></div>
            <div className="card"><div className="label">Blood type</div><div className="value" style={{ fontSize: 18 }}>{s.blood_type || '—'}</div></div>
          </div>
          <div style={{ marginTop: 8 }}>
            <label style={{ marginRight: 8 }}>Blood type:{' '}
              <select value={s.blood_type || ''} onChange={(e) => setField('blood_type', e.target.value)}>
                <option value="">—</option>
                {['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', '0+', '0-', 'unknown'].map((b) => (
                  <option key={b} value={b}>{b}</option>
                ))}
              </select>
            </label>
            <label>
              Organ donor:{' '}
              <select value={s.organ_donor || ''} onChange={(e) => setField('organ_donor', e.target.value)}>
                <option value="">—</option>
                <option value="yes">yes</option>
                <option value="no">no</option>
                <option value="unknown">unknown</option>
              </select>
            </label>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="form-card">
          <h3 style={{ marginTop: 0 }}>Current medications</h3>
          <p className="muted" style={{ marginTop: 0, fontSize: 12 }}>
            Read-only — managed on the <Link to="/medications">Meds</Link> page.
          </p>
          <ul>
            {d.current_medications.length === 0 && <li className="muted">None.</li>}
            {d.current_medications.map((m: any) => (
              <li key={m.id}>
                {m.name} {m.dose_amount ?? ''} {m.dose_unit || ''}
              </li>
            ))}
          </ul>
        </div>
      </section>

      {LIST_SECTIONS.map((section) => {
        const entries: Entry[] = s[section] || []
        return (
          <section className="section" key={section}>
            <div className="form-card">
              <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                <h3 style={{ margin: 0 }}>
                  {SECTION_LABELS[section]} {card.stale_sections.includes(section) && <span title="stale">⚠️</span>}
                </h3>
                <button className="secondary" style={{ fontSize: 11 }} onClick={() => setAddingSection(addingSection === section ? null : section)}>
                  {addingSection === section ? 'Cancel' : '+ Add'}
                </button>
              </div>
              {addingSection === section && (
                <div className="form-actions" style={{ marginTop: 8 }}>
                  <input
                    value={draft}
                    placeholder={NAME_FIELD[section]}
                    onChange={(e) => setDraft(e.target.value)}
                    style={{ flex: 1 }}
                  />
                  <button disabled={!draft.trim()} onClick={() => addEntry(section)}>Save</button>
                </div>
              )}
              <ul>
                {entries.length === 0 && <li className="muted">None.</li>}
                {entries.map((e) => (
                  <li key={e.id}>
                    {String(e[NAME_FIELD[section]] ?? JSON.stringify(e))}
                    {' '}
                    <button className="danger" style={{ fontSize: 10, padding: '1px 6px' }} onClick={() => removeEntry(section, e.id)}>
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </section>
        )
      })}
    </div>
  )
}
