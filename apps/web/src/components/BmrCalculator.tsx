import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'

/*
 * BMR calculator. Katch–McArdle:
 *   BMR = 370 + 21.6 × LBM(kg),  LBM = weight × (1 − bodyfat/100)
 *
 * This began life in `components/ActivityBalancePopups.tsx`, which was imported
 * nowhere, so it never reached a build. Backed by GET/PATCH
 * /api/v1/activity/settings (basal_metabolism_kcal); the value applies to all
 * days.
 *
 * Layout note: the popup is narrow on phones, so fields are laid out as two
 * short rows (given data, then the result + save) rather than one row of three
 * controls, which squashed the inputs into unreadable slivers.
 */

function katchMcardle(weightKg: number, bodyFatPct: number): number {
  const lbm = weightKg * (1 - bodyFatPct / 100)
  return 370 + 21.6 * lbm
}

export default function BmrCalculator({ onSaved }: { onSaved?: () => void }) {
  const [bmr, setBmr] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [weight, setWeight] = useState('')
  const [fat, setFat] = useState('')
  const [result, setResult] = useState<number | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    authClient.api('/activity/settings')
      .then((s) => {
        setBmr(s?.basal_metabolism_kcal ?? null)
        if (s?.basal_metabolism_kcal) setDraft(String(Math.round(s.basal_metabolism_kcal)))
      })
      .catch(() => {})
  }, [])

  function calculate() {
    const w = parseFloat(weight)
    const f = parseFloat(fat)
    if (!Number.isFinite(w) || !Number.isFinite(f) || w <= 0 || w > 500 || f < 0 || f >= 60) {
      setErr('Enter a weight in kg and a body fat % between 0 and 60.')
      return
    }
    setErr(null)
    const v = Math.round(katchMcardle(w, f))
    setResult(v)
    setDraft(String(v))
  }

  async function save() {
    const v = parseFloat(draft)
    if (!Number.isFinite(v) || v < 500 || v > 6000) {
      setErr('BMR must be between 500 and 6000 kcal.')
      return
    }
    setBusy(true)
    setErr(null)
    setMsg(null)
    try {
      await authClient.api('/activity/settings', {
        method: 'PATCH',
        body: JSON.stringify({ basal_metabolism_kcal: v }),
      })
      setBmr(v)
      setMsg('BMR saved — applies to all days.')
      onSaved?.()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="section manual-entry-section">
      <h3>BMR calculator</h3>
      <p className="muted">
        {bmr != null ? <>Current: <strong>{Math.round(bmr)} kcal</strong>. </> : 'Not set yet. '}
        Katch–McArdle: 370 + 21.6 × lean body mass.
      </p>

      <div className="quick-entry-row">
        <input type="number" inputMode="decimal" step="0.1" value={weight}
          onChange={(e) => setWeight(e.target.value)} placeholder="Weight (kg)" />
        <input type="number" inputMode="decimal" step="0.1" value={fat}
          onChange={(e) => setFat(e.target.value)} placeholder="Body fat (%)" />
      </div>
      <div className="quick-entry-row" style={{ marginTop: 8 }}>
        <button className="small" onClick={calculate}>Calculate</button>
        {result != null && <span className="muted" style={{ alignSelf: 'center' }}>
          → <strong>{result} kcal</strong>
        </span>}
      </div>

      <div className="quick-entry-row" style={{ marginTop: 8 }}>
        <input type="number" inputMode="decimal" value={draft}
          onChange={(e) => setDraft(e.target.value)} placeholder="BMR (kcal)" />
        <button className="small" onClick={save} disabled={busy}>{busy ? '…' : 'Save'}</button>
      </div>

      {err && <div className="error" style={{ marginTop: 8 }}>{err}</div>}
      {msg && <div className="status" style={{ marginTop: 8 }}>{msg}</div>}
    </section>
  )
}
