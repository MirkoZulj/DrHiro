/**
 * Auth client: Telegram Mini App initData exchange + token storage.
 *
 * Server-side validation happens at POST /api/v1/auth/telegram-miniapp.
 * We never trust client-side user data; initData is forwarded verbatim.
 *
 * Session model: the backend issues a short-lived access token (15 min) and
 * a long-lived refresh token (30 days). This client stores BOTH, and
 * transparently refreshes the access token on a 401 so the user stays signed
 * in for up to 30 days without re-entering a code. It also supports the
 * Telegram magic-link flow (POST /auth/telegram-link/start + /complete) so a
 * link sent by the bot signs the user in with no OTP.
 */

export interface TelegramUser {
  id: string
  first_name?: string
}

const ACCESS_KEY = 'drhiro_access_token'
const REFRESH_KEY = 'drhiro_refresh_token'

// API base baked at build time (VITE_API_BASE). Dev default '/api/v1' (vite
// proxy -> local API); prod '/drhiro/api/v1' (VPS nginx under /drhiro/).
// All auth + data fetches must go through this so login works behind the
// subpath — a hardcoded '/api/v1' falls through to the SPA catch-all and
// returns 405 on POST.
export const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1'

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData: string
        ready: () => void
        expand: () => void
        colorScheme?: string
        setHeaderColor?: (c: string) => void
      }
    }
  }
}

function storeTokens(data: { access_token: string; refresh_token?: string; user_id?: string }) {
  localStorage.setItem(ACCESS_KEY, data.access_token)
  if (data.refresh_token) localStorage.setItem(REFRESH_KEY, data.refresh_token)
}

export const authClient = {
  isTelegram(): boolean {
    return typeof window !== 'undefined' && !!window.Telegram?.WebApp?.initData
  },

  /** True when we hold a session token (access or refresh). */
  hasSession(): boolean {
    return !!(this.token() || this.refreshToken())
  },

  /**
   * Refresh the access token using the stored refresh token. Returns true on
   * success. Clears the session on a failed refresh (refresh expired/revoked).
   */
  async refresh(): Promise<boolean> {
    const refresh = this.refreshToken()
    if (!refresh) return false
    try {
      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      })
      if (!res.ok) {
        this.clear()
        return false
      }
      const data = await res.json()
      storeTokens(data)
      return true
    } catch {
      return false
    }
  },

  /**
   * Complete a Telegram magic-link login: the bot mints a link code and sends
   * it to the user's Telegram; tapping the link opens the app and calls this
   * with the code to exchange it for tokens (no OTP entry).
   */
  async completeTelegramLink(linkCode: string): Promise<TelegramUser | null> {
    const res = await fetch(`${API_BASE}/auth/telegram-link/complete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ link_code: linkCode }),
    })
    if (!res.ok) return null
    const data = await res.json()
    storeTokens(data)
    return { id: data.user_id, first_name: 'Telegram user' }
  },

  async init(): Promise<TelegramUser | null> {
    // If we already hold an access token, return a session — a 401 on the
    // first API call triggers an automatic refresh.
    if (this.token()) {
      return { id: 'session' }
    }
    // No access token but we have a refresh token -> try to refresh, which
    // restores the session without re-authenticating.
    if (this.refreshToken()) {
      const ok = await this.refresh()
      return ok ? { id: 'session' } : null
    }
    // Telegram Mini App path.
    if (this.isTelegram()) {
      const tg = window.Telegram!.WebApp!
      tg.ready()
      tg.expand()
      return this.exchangeInitData(tg.initData)
    }
    return null
  },

  async exchangeInitData(initData: string): Promise<TelegramUser | null> {
    const res = await fetch(`${API_BASE}/auth/telegram-miniapp`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ init_data: initData }),
    })
    if (!res.ok) return null
    const data = await res.json()
    storeTokens(data)
    return { id: data.user_id, first_name: 'Telegram user' }
  },

  /** Store tokens from the OTP verify flow (see Login.tsx). */
  setSession(data: { access_token: string; refresh_token?: string; user_id?: string }) {
    storeTokens(data)
  },

  token(): string | null {
    return localStorage.getItem(ACCESS_KEY)
  },

  refreshToken(): string | null {
    return localStorage.getItem(REFRESH_KEY)
  },

  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
  },

  async api(path: string, options: RequestInit = {}): Promise<any> {
    const base = API_BASE
    const doFetch = (token: string | null) =>
      fetch(`${base}${path}`, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
          ...(options.headers || {}),
        },
      })

    let res = await doFetch(this.token())

    // Access token expired -> transparently refresh once and retry.
    if (res.status === 401 && this.refreshToken()) {
      const refreshed = await this.refresh()
      if (refreshed) {
        res = await doFetch(this.token())
      }
    }

    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`)
    return res.json()
  },
}
