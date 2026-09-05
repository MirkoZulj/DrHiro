import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'
import { Consents as ConsentsData, Consent } from '../lib/types'
import { titleCase } from '../lib/format'

export default function Consents() {
  const [data, setData] = useState<ConsentsData | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [grantee, setGrantee] = useState('')
  const [scope, setScope] = useState('weight')
  const [access, setAccess] = useState('read')
  const [creating, setCreating] = useState(false)

  async function load() {
    setError(null)
    try {
      setData(await authClient.api('/consents'))
    } catch (e) {
      setError(String(e))
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function createConsent() {
    if (!grantee.trim()) return
    setCreating(true)
    setError(null)
    try {
      await authClient.api('/consents', {
        method: 'POST',
        body: JSON.stringify({ grantee_user_id: grantee.trim(), scope, access_level: access }),
      })
      setGrantee('')
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setCreating(false)
    }
  }

  function renderRows(items: Consent[], relation: 'by_me' | 'to_me') {
    if (items.length === 0)
      return <div className="empty">Nothing shared {relation === 'by_me' ? 'by you yet' : 'with you yet'}.</div>
    return (
      <ul>
        {items.map((c) => (
          <li key={c.id} className="list-item">
            <div className="main">
              <div className="title">{titleCase(c.scope)}</div>
              <div className="desc">
                {relation === 'by_me' ? (
                  <>→ {c.grantee_user_id}</>
                ) : (
                  <>from {c.grantor_user_id}</>
                )}{' '}
                · <span className="tag">{c.access_level}</span>
              </div>
            </div>
          </li>
        ))}
      </ul>
    )
  }

  return (
    <div className="consents">
      <h2>Privacy &amp; sharing</h2>
      <p className="page-sub">Your data is private by default. Share specific health data with people you trust.</p>

      {error && <div className="error">{error}</div>}

      <section className="section">
        <h3>Grant new access</h3>
        <div className="form-card">
          <div className="field-row">
            <div className="field">
              <label>Recipient user ID</label>
              <input value={grantee} onChange={(e) => setGrantee(e.target.value)} placeholder="user-id" />
            </div>
          </div>
          <div className="field-row">
            <div className="field">
              <label>Scope</label>
              <select value={scope} onChange={(e) => setScope(e.target.value)}>
                {['weight', 'bp', 'steps', 'meals', 'sleep', 'all'].map((s) => (
                  <option key={s} value={s}>
                    {titleCase(s)}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Access level</label>
              <select value={access} onChange={(e) => setAccess(e.target.value)}>
                <option value="read">Read only</option>
                <option value="write">Read &amp; write</option>
              </select>
            </div>
          </div>
          <div className="form-actions">
            <button onClick={createConsent} disabled={creating || !grantee.trim()}>
              {creating ? 'Granting…' : 'Grant access'}
            </button>
          </div>
        </div>
      </section>

      <section className="section">
        <h3>Shared by me</h3>
        {data ? renderRows(data.granted_by_me, 'by_me') : <div className="loading">Loading…</div>}
      </section>

      <section className="section">
        <h3>Shared with me</h3>
        {data ? renderRows(data.granted_to_me, 'to_me') : <div className="loading">Loading…</div>}
      </section>

      <p className="note">Default is private. Spouse access is read-only unless explicitly granted.</p>
    </div>
  )
}
