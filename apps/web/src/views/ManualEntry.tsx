import { useState } from 'react'
import { authClient } from '../lib/auth'
import { ManualTextResult } from '../lib/types'
import { titleCase } from '../lib/format'

const EXAMPLES = ['78.5 kg', '120/80', '128 / 82 with pulse 64', '2.5 liters of water', '500 ml water']

export default function ManualEntry() {
  const [text, setText] = useState('')
  const [result, setResult] = useState<ManualTextResult | null>(null)
  const [status, setStatus] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!text.trim()) return
    setError(null)
    setStatus('')
    setResult(null)
    setBusy(true)
    try {
      const res = await authClient.api('/ingest/manual/text', {
        method: 'POST',
        body: JSON.stringify({ text: text.trim() }),
      })
      setResult(res)
      if (res && res.accepted > 0) setStatus('Saved successfully.')
    } catch (e) {
      const msg = String(e)
      // 422 = nothing parsed
      if (msg.includes('422')) {
        setError('Nothing could be parsed from that text. Try one of the examples below.')
      } else {
        setError(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="entry">
      <h2>Manual entry</h2>
      <p className="page-sub">Describe your measurement in one line — we'll parse it for you.</p>

      {error && <div className="error">{error}</div>}
      {status && <div className="status">{status}</div>}

      <section className="section">
        <div className="form-card">
          <div className="field">
            <label htmlFor="describe-text">Describe your measurement</label>
            <textarea
              id="describe-text"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit()
              }}
              placeholder="e.g. 78.5 kg"
              autoFocus
            />
          </div>
          <div className="form-actions">
            <button onClick={submit} disabled={busy || !text.trim()}>
              {busy ? <span className="spinner" /> : null}Save
            </button>
          </div>

          <p className="muted">Try: {EXAMPLES.join(' · ')}</p>
        </div>
      </section>

      {result && (
        <section className="section">
          <h3>Result</h3>
          <div className="form-card">
            {result.measurements && result.measurements.length > 0 ? (
              <ul className="metric-list">
                {result.measurements.map((p, i) => (
                  <li key={i} className="list-item">
                    <div className="main">
                      <div className="title">{titleCase(p.metric_type)}</div>
                      <div className="desc mono">{JSON.stringify(p.value_json)}</div>
                    </div>
                    <div className="value">{describeValue(p)}</div>
                  </li>
                ))}
              </ul>
            ) : (
              <div className="empty">No measurements parsed.</div>
            )}

            {result.unparsed && (
              <p className="note warn" style={{ marginTop: 12 }}>
                <strong>Not recognised:</strong> {result.unparsed}
              </p>
            )}
            {!result.unparsed && result.accepted > 0 && (
              <p className="note muted" style={{ marginTop: 12 }}>
                {result.accepted} measurement(s) accepted.
              </p>
            )}
          </div>
        </section>
      )}
    </div>
  )
}

/** Human-readable single line for a parsed measurement, from its value_json. */
function describeValue(m: { metric_type: string; value_json: Record<string, unknown> }) {
  const v = m.value_json ?? {}
  const t = (m.metric_type || '').toLowerCase()
  if (t === 'weight') return `${v.weight_kg} kg`
  if (t === 'blood_pressure') {
    const pulse = v.pulse_bpm ? ` · ${v.pulse_bpm} bpm` : ''
    return `${v.systolic_mmhg}/${v.diastolic_mmhg}${pulse}`
  }
  if (t === 'water') return `${v.amount_ml} ml`
  return JSON.stringify(v)
}
