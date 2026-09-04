import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'

interface MaskedSecret { set: boolean }

interface SettingsData {
  ai_backend_url: string
  model_name: string
  ai_api_key: MaskedSecret
  telegram_bot_token: MaskedSecret
  telegram_allowed_username: string
}

const EMPTY: SettingsData = {
  ai_backend_url: '',
  model_name: '',
  ai_api_key: { set: false },
  telegram_bot_token: { set: false },
  telegram_allowed_username: '',
}

/**
 * System / instance settings (runtime settings store).
 *
 * Only the authorized user can read/write these — the API returns 403
 * otherwise and this block shows a "not authorized" note. Secret fields
 * (AI API key, Telegram bot token) are write-only: the server returns only a
 * set/not-set indicator, and the UI only ever sends a NEW value (or clears).
 * Existing secrets are never loaded into the input.
 */
export default function SystemSettings() {
  const [data, setData] = useState<SettingsData>(EMPTY)
  const [loaded, setLoaded] = useState(false)
  const [authorized, setAuthorized] = useState<boolean | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedMsg, setSavedMsg] = useState('')
  const [error, setError] = useState<string | null>(null)
  // New secret values (only sent on save; never read back).
  const [aiKeyNew, setAiKeyNew] = useState('')
  const [botTokenNew, setBotTokenNew] = useState('')

  useEffect(() => {
    authClient.api('/settings')
      .then((s) => { setData(s); setAuthorized(true); setLoaded(true) })
      .catch((e) => {
        const msg = String(e)
        setAuthorized(msg.includes('403'))
        setLoaded(true)
        if (!msg.includes('403')) setError('Could not load settings: ' + msg)
      })
  }, [])

  async function save() {
    setError(null); setSavedMsg(''); setSaving(true)
    try {
      const payload: any = {}
      if (data.ai_backend_url !== undefined) payload.ai_backend_url = data.ai_backend_url
      if (data.model_name !== undefined) payload.model_name = data.model_name
      if (data.telegram_allowed_username !== undefined) payload.telegram_allowed_username = data.telegram_allowed_username
      if (aiKeyNew) payload.ai_api_key = { set: true, value: aiKeyNew }
      if (botTokenNew) payload.telegram_bot_token = { set: true, value: botTokenNew }
      if (!data.ai_api_key.set && !aiKeyNew) payload.ai_api_key = { set: false }
      if (!data.telegram_bot_token.set && !botTokenNew) payload.telegram_bot_token = { set: false }
      const res = await authClient.api('/settings', { method: 'PUT', body: JSON.stringify(payload) })
      setData(res)
      setAiKeyNew(''); setBotTokenNew('')
      // Determine which services will be restarted so the user understands the
      // apply model. (Field-name only; never a value.)
      const needsBridge = ('telegram_bot_token' in payload) || ('telegram_allowed_username' in payload)
      const needsOpenclaw = ('telegram_bot_token' in payload) || ('model_name' in payload) ||
        ('ai_backend_url' in payload) || ('ai_api_key' in payload)
      const needsTrueforge = ('model_name' in payload) || ('ai_backend_url' in payload) || ('ai_api_key' in payload)
      const notes: string[] = []
      if (needsBridge) notes.push('Telegram bridge restarting — ~10 seconds')
      if (needsOpenclaw) notes.push('OpenClaw gateway restarting — ~10 seconds')
      if (needsTrueforge) notes.push('TrueForge re-provisioning (agent + model) — ~30 seconds')
      setSavedMsg(notes.length
        ? 'Saved. ' + notes.join(' · ')
        : 'Saved. Applied immediately.')
    } catch (e) {
      setError('Save failed: ' + String(e))
    } finally {
      setSaving(false)
    }
  }

  if (!loaded) return null
  if (authorized === false) {
    return (
      <section className="section">
        <h3>System settings</h3>
        <div className="form-card">
          <p className="muted" style={{ marginTop: 0 }}>
            Only the authorized user can change system settings. Your account is not the configured
            administrator.
          </p>
        </div>
      </section>
    )
  }

  return (
    <section className="section">
      <h3>System settings</h3>
      <div className="form-card">
        <p className="muted" style={{ marginTop: 0 }}>
          Runtime configuration for this drHiro instance. AI backend, model, and Telegram are applied on
          re-provision / restart — changes are not live.
        </p>

        <div className="field">
          <label>AI backend URL</label>
          <input value={data.ai_backend_url} onChange={(e) => setData({ ...data, ai_backend_url: e.target.value })} placeholder="https://api.openai.com/v1" />
        </div>

        <div className="field">
          <label>Model name</label>
          <input value={data.model_name} onChange={(e) => setData({ ...data, model_name: e.target.value })} placeholder="gpt-4o-mini" />
        </div>

        <div className="field">
          <label>AI API key {data.ai_api_key.set ? '(set)' : '(not set)'}</label>
          <input type="password" value={aiKeyNew} onChange={(e) => setAiKeyNew(e.target.value)} placeholder={data.ai_api_key.set ? 'Leave blank to keep current' : 'Enter API key'} autoComplete="new-password" />
        </div>

        <div className="field">
          <label>Telegram bot token {data.telegram_bot_token.set ? '(set)' : '(not set)'}</label>
          <input type="password" value={botTokenNew} onChange={(e) => setBotTokenNew(e.target.value)} placeholder={data.telegram_bot_token.set ? 'Leave blank to keep current' : 'Enter bot token'} autoComplete="new-password" />
        </div>

        <div className="field">
          <label>Authorized Telegram username</label>
          <input value={data.telegram_allowed_username} onChange={(e) => setData({ ...data, telegram_allowed_username: e.target.value })} placeholder="alice (no @)" />
        </div>

        <div className="form-actions" style={{ marginTop: 12 }}>
          <button onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save settings'}</button>
        </div>
        {savedMsg && <div className="status">{savedMsg}</div>}
        {error && <div className="error">{error}</div>}
      </div>
    </section>
  )
}
