import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'
// drHiro design system tokens (calm, clinical, trustworthy). Imported after
// index.css so the designed palette + semantic colors override component
// defaults. Light = :root, dark = [data-theme="dark"].
import './design/tokens.css'

// Respect the user's OS color-scheme preference (design-system.md: theme
// switching). Defaults to light unless prefers-color-scheme is dark.
const mq = window.matchMedia('(prefers-color-scheme: dark)')
document.documentElement.dataset.theme = mq.matches ? 'dark' : 'light'
mq.addEventListener('change', (e) => {
  document.documentElement.dataset.theme = e.matches ? 'dark' : 'light'
})

// Subpath deployment (e.g. /drhiro-app/ behind nginx). Vite's `base` and the
// router `basename` must agree; VITE_BASE_PATH is baked at build time.
const basePath = import.meta.env.VITE_BASE_PATH || '/'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename={basePath}>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
