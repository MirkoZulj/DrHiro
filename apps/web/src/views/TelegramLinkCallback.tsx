import { useEffect, useState } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import { authClient, TelegramUser } from '../lib/auth'

/**
 * Telegram magic-link login callback.
 *
 * The drHiro bot can mint a short link code (POST /auth/telegram-link/start)
 * and send the user a URL like .../drhiro-app/auth/link?link=CODE in Telegram.
 * Tapping it opens this route, which exchanges the code for real tokens
 * (POST /auth/telegram-link/complete) with no OTP entry, then redirects to
 * the dashboard.
 */
export default function TelegramLinkCallback({
  onLogin,
}: {
  onLogin?: (user: TelegramUser | null) => void
}) {
  const [params] = useSearchParams()
  const [state, setState] = useState<'working' | 'ok' | 'failed'>('working')
  const link = params.get('link') || params.get('code') || ''

  useEffect(() => {
    if (!link) {
      setState('failed')
      return
    }
    authClient.completeTelegramLink(link).then((u) => {
      if (u) onLogin?.(u)
      setState(u ? 'ok' : 'failed')
    })
  }, [link, onLogin])

  if (state === 'ok') return <Navigate to="/" replace />

  return (
    <div className="login">
      <h2>Signing you in…</h2>
      <p className="muted">
        {state === 'working' ? 'Connecting your Telegram account…' : 'This link is invalid or has expired. Ask the bot for a fresh sign-in link.'}
      </p>
    </div>
  )
}
