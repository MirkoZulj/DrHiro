import { ReactNode, useEffect, useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'

interface NavItem {
  to: string
  label: string
  icon: string
  end?: boolean
}

interface AppShellProps {
  user: { id?: string; first_name?: string } | null
  nav: NavItem[]
  children: ReactNode
}

export function AppShell({ user, nav, children }: AppShellProps) {
  const navigate = useNavigate()
  const [quickOpen, setQuickOpen] = useState(false)
  const [theme, setTheme] = useState<string>(() =>
    (typeof document !== 'undefined' ? document.documentElement.dataset.theme : 'light') || 'light',
  )

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try { localStorage.setItem('drh-theme', theme) } catch { /* ignore */ }
  }, [theme])

  const quickActions = [
    { icon: '🍽️', label: 'Log meal', to: '/meals' },
    { icon: '⏰', label: 'Add reminder', to: '/reminders' },
    { icon: '🏃', label: 'Log activity', to: '/activities' },
  ]

  const runQuick = (to: string) => {
    setQuickOpen(false)
    navigate(to)
  }

  const initial = (user?.first_name || 'K').charAt(0).toUpperCase()

  return (
    <div className="app-shell">
      {/* ===== Desktop sidebar ===== */}
      <aside className="sidebar" aria-label="Main navigation">
        <div className="side-brand">drHiro<span className="dot">.</span></div>
        <nav className="side-nav">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `side-tab${isActive ? ' active' : ''}`}
            >
              <span className="ticon" aria-hidden="true">{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="side-foot">
          <span className="avatar" aria-hidden="true">{initial}</span>
          <div className="side-foot-who">
            <div className="who">{user?.first_name || 'Signed in'}</div>
            <div className="sub">{user ? 'drHiro' : ''}</div>
          </div>
          <button
            className="theme-btn"
            aria-label="Toggle dark mode"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          >{theme === 'dark' ? '☀️' : '🌙'}</button>
        </div>
      </aside>

      {/* ===== Main column ===== */}
      <div className="app">
        <header className="topbar">
          <span className="brand">drHiro<span className="dot">.</span></span>
          <div className="topbar-right">
            <button
              className="theme-btn"
              aria-label="Toggle dark mode"
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
            >{theme === 'dark' ? '☀️' : '🌙'}</button>
            <span className="avatar" aria-hidden="true">{initial}</span>
          </div>
        </header>

        {children}

        {/* ===== Mobile bottom tabbar ===== */}
        <nav className="tabbar" aria-label="Main navigation">
          {nav.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `tab${isActive ? ' active' : ''}`}
            >
              <span className="ticon" aria-hidden="true">{item.icon}</span>
              <span className="tlabel">{item.label}</span>
            </NavLink>
          ))}
        </nav>

        {/* ===== FAB + quick menu ===== */}
        <div className={`qmenu${quickOpen ? ' open' : ''}`} role="menu" aria-label="Quick actions">
          {quickActions.map((q) => (
            <button key={q.label} className="qitem" role="menuitem" onClick={() => runQuick(q.to)}>
              <span className="qicon" aria-hidden="true">{q.icon}</span>{q.label}
            </button>
          ))}
        </div>
        <button
          className="fab"
          aria-label="Quick actions"
          aria-expanded={quickOpen}
          onClick={() => setQuickOpen((o) => !o)}
        >{quickOpen ? '×' : '+'}</button>
      </div>
    </div>
  )
}
