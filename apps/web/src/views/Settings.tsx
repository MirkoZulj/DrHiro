import { useState } from 'react'
import { authClient, API_BASE } from '../lib/auth'

export default function Settings() {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [exportInfo, setExportInfo] = useState('')
  const [delMsg, setDelMsg] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)

  async function exportData() {
    setError(null)
    setExportInfo('')
    setBusy(true)
    try {
      const res = await fetch(`${API_BASE}/exports`, {
        headers: { Authorization: `Bearer ${authClient.token()}` },
      })
      if (!res.ok) throw new Error(`Export ${res.status}`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `drhiro-export-${new Date().toISOString().slice(0, 10)}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
      setExportInfo('Your data export is ready.')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  async function requestDeletion() {
    setError(null)
    setDelMsg('')
    setBusy(true)
    try {
      await authClient.api('/account/deletion-request', { method: 'POST' })
      setDelMsg('Deletion requested. Our team will follow up to confirm before anything is removed.')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="settings">
      <h2>Settings</h2>
      <p className="page-sub">Manage your data and account.</p>

      {error && <div className="error">{error}</div>}

      <section className="section">
        <h3>Your data</h3>
        <div className="form-card">
          <p className="muted" style={{ marginTop: 0 }}>
            Download everything drHiro stores about you as JSON.
          </p>
          <div className="form-actions" style={{ marginTop: 12 }}>
            <button onClick={exportData} disabled={busy}>
              {busy ? 'Preparing…' : 'Export data'}
            </button>
          </div>
          {exportInfo && <div className="status">{exportInfo}</div>}
        </div>
      </section>

      <section className="section">
        <h3>Account</h3>
        <div className="form-card">
          <p className="muted" style={{ marginTop: 0 }}>
            Request permanent deletion of your drHiro account and all associated health data. We'll confirm with you
            before anything is removed.
          </p>
          {confirmDelete ? (
            <div className="form-actions" style={{ marginTop: 12 }}>
              <button className="danger" onClick={requestDeletion} disabled={busy}>
                {busy ? 'Requesting…' : 'Yes, delete my account'}
              </button>
              <button className="secondary" onClick={() => setConfirmDelete(false)}>
                Cancel
              </button>
            </div>
          ) : (
            <div className="form-actions" style={{ marginTop: 12 }}>
              <button className="danger" onClick={() => setConfirmDelete(true)}>
                Request account deletion
              </button>
            </div>
          )}
          {delMsg && <div className="status">{delMsg}</div>}
        </div>
      </section>
    </div>
  )
}
