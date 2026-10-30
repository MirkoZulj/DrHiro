import { useEffect, useState } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { authClient, TelegramUser } from './lib/auth'
import Home from './views/Home'
import Login from './views/Login'
import Meals from './views/Meals'
import Reminders from './views/Reminders'
import Goals from './views/Goals'
import Consents from './views/Consents'
import Settings from './views/Settings'
import Activities from './views/Activities'
import TelegramLinkCallback from './views/TelegramLinkCallback'
import { AppShell } from './components/AppShell'

const NAV = [
  { to: '/', label: 'Home', icon: '🏠', end: true },
  { to: '/activities', label: 'Activities', icon: '🏃' },
  { to: '/meals', label: 'Meals', icon: '🍽️' },
  { to: '/reminders', label: 'Reminders', icon: '⏰' },
  { to: '/goals', label: 'Goals', icon: '🎯' },
  { to: '/settings', label: 'Settings', icon: '⚙️' },
]

export default function App() {
  const [user, setUser] = useState<TelegramUser | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    authClient.init().then((u) => {
      setUser(u)
      setReady(true)
    })
  }, [])

  if (!ready) return <div className="loading">Loading drHiro…</div>

  return (
    <AppShell user={user} nav={NAV}>
      <main className="page">
        <Routes>
          <Route path="/" element={user ? <Home /> : <Navigate to="/login" />} />
          <Route path="/activities" element={user ? <Activities /> : <Navigate to="/login" />} />
          <Route path="/login" element={user ? <Navigate to="/" /> : <Login onLogin={setUser} />} />
          <Route path="/auth/link" element={<TelegramLinkCallback onLogin={setUser} />} />
          <Route path="/trends" element={<Navigate to="/" replace />} />
          <Route path="/meals" element={user ? <Meals /> : <Navigate to="/login" />} />
          <Route path="/reminders" element={user ? <Reminders /> : <Navigate to="/login" />} />
          <Route path="/goals" element={user ? <Goals /> : <Navigate to="/login" />} />
          <Route path="/consents" element={user ? <Consents /> : <Navigate to="/login" />} />
          <Route path="/settings" element={user ? <Settings /> : <Navigate to="/login" />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </main>
    </AppShell>
  )
}
