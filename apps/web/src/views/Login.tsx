import { useState } from 'react'
import { TelegramUser, API_BASE, authClient } from '../lib/auth'

/**
 * Passwordless web login:
 * 1. User enters their drHiro display name.
 * 2. API sends a 6-digit code to their linked Telegram/WhatsApp.
 * 3. User enters the code; API issues tokens.
 */
export default function Login({ onLogin }: { onLogin: (u: TelegramUser) => void }) {
  const [step, setStep] = useState<'name' | 'code'>('name')
  const [identifier, setIdentifier] = useState('')
  const [code, setCode] = useState('')
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')

  async function requestCode() {
    setError('')
    setStatus('Sending code…')
    try {
      const res = await fetch(`${API_BASE}/auth/web/otp/request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || 'Request failed')
      }
      const data = await res.json()
      setStatus(
        data.sent_to === 'telegram'
          ? `Code sent to your Telegram. It expires in ${data.expires_in_seconds / 60} minutes.`
          : 'Code request accepted.',
      )
      setStep('code')
    } catch (e) {
      setError(String(e))
      setStatus('')
    }
  }

  async function verifyCode() {
    setError('')
    setStatus('Verifying…')
    try {
      const res = await fetch(`${API_BASE}/auth/web/otp/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier, code }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || 'Verification failed')
      }
      const data = await res.json()
      // Store both access + refresh tokens so the session survives beyond the
      // 15-min access lifetime (auto-refresh keeps you signed in for 30 days).
      authClient.setSession(data)
      onLogin({ id: data.user_id })
    } catch (e) {
      setError(String(e))
      setStatus('')
    }
  }

  if (step === 'code') {
    return (
      <div className="login">
        <h2>Enter the code</h2>
        <p className="muted">A 6-digit code was sent to your Telegram.</p>
        <input
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
          placeholder="6-digit code"
          inputMode="numeric"
          autoFocus
        />
        <button onClick={verifyCode} disabled={code.length !== 6}>
          Sign in
        </button>
        {status && <p className="status">{status}</p>}
        {error && <p className="error">{error}</p>}
        <button className="link" onClick={() => setStep('name')}>
          ← Use a different name
        </button>
      </div>
    )
  }

  return (
    <div className="login">
      <h2>Welcome to drHiro</h2>
      <p className="muted">Enter your drHiro display name. We'll send a sign-in code to your Telegram.</p>
      <input
        value={identifier}
        onChange={(e) => setIdentifier(e.target.value)}
        placeholder="Your name"
        onKeyDown={(e) => e.key === 'Enter' && identifier.trim() && requestCode()}
        autoFocus
      />
      <button onClick={requestCode} disabled={!identifier.trim()}>
        Send code
      </button>
      {status && <p className="status">{status}</p>}
      {error && <p className="error">{error}</p>}
      <p className="note">
        New here? Start the drHiro bot on Telegram and pair your account first.
      </p>
      <p className="note">
        Prefer a direct sign-in? Ask the drHiro bot on Telegram for a sign-in link — tap it and you're in, no code needed.
      </p>
    </div>
  )
}
