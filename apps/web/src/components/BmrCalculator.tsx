import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'

/*
 * BMR calculator. Katch–McArdle:
 *   BMR = 370 + 21.6 × LBM(kg),  LBM = weight × (1 − bodyfat/100)
 *
 * This existed as `components/ActivityBalancePopups.tsx` but that module was
 * imported nowhere, so the calculator never reached a build. It lives here as a
 * self-contained component so the Activity popup can render it.
 *
 * Backed by GET/PATCH /api/v1/activity/settings (basal_metabolism_kcal), which
 * applies to all days.
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
    if (!Number.isFinite(w) || !Number.isFinite(f) || w <= 0 || f < 0 || f >= 60) {
      setMsg('Enter a valid weight (kg) and body fat (%).')
      return
    }
    const v = Math.round(katchMcardle(w, f))
    setResult(v)
    setDraft(String(v))
    setMsg(null)
  }

  async function save() {
    const v = parseFloat(draft)
    if (!Number.isFinite(v) || v < 500 || v > 6000) {
      setMsg('BMR must be between 500 and 6000 kcal.')
      return
    }
    setBusy(true)
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
      setMsg(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="section">
      <h3>BMR calculator</h3>
      <p className="popup-hint">
        {bmr != null
          ? `Current BMR: ${Math.round(bmr)} kcal. `
          : 'BMR not set yet. '}
        Katch–McArdle: 370 + 21.6 × lean body mass.
      </p>
      <div className="quick-entry-row">
        <input type="number" inputMode="decimal" value={weight}
          onChange={(e) => setWeight(e.target.value)} placeholder="Weight (kg)" />
        <input type="number" inputMode="decimal" value={fat}
          onChange={(e) => setFat(e.target.value)} placeholder="Body fat (%)" />
        <button className="small" onClick={calculate}>Calculate</button>
      </div>
      {result != null && (
        <p className="popup-hint">Calculated BMR: <strong>{result} kcal</strong></p>
      )}
      <div className="quick-entry-row">
        <input type="number" inputMode="decimal" value={draft}
          onChange={(e) => setDraft(e.target.value)} placeholder="BMR (kcal)" />
        <button className="small" onClick={save} disabled={busy}>{busy ? '…' : 'Save BMR'}</button>
      </div>
      {msg && <p className="popup-hint">{msg}</p>}
    </section>
  )
}
