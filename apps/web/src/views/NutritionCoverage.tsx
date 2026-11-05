import { useEffect, useState } from 'react'
import { authClient } from '../lib/auth'

interface CoverageRow {
  key: string
  name: string
  rda: number
  unit: string
  from_supplements: number
  from_food: number | null
  coverage_pct: number | null
  source: string
  age: number
}

/** Wave 4 (0.10.3): Intake analysis — daily %RDA progress bars.
 *  Supplements (taken doses) + food/drinks combined; food
 *  micronutrients the DB does not store contribute supplement-only,
 *  and missing data renders '—', never a fabricated number. */
export default function NutritionCoverage() {
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10))
  const [rows, setRows] = useState<CoverageRow[]>([])
  const [age, setAge] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    authClient
      .api(`/nutrition-coverage?date=${date}`)
      .then((res) => {
        setRows(res.rows || [])
        setAge(res.rows?.[0]?.age ?? null)
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [date])

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ margin: '8px 0' }}>Intake analysis</h3>
        <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
      </div>
      <p className="muted" style={{ fontSize: 12 }}>
        % of daily required amount (adult male{age !== null ? `, age ${age}` : ''}):
        supplements taken + food/drinks logged. Food micronutrients not
        stored in the food DB show supplement-only contribution.
      </p>
      {error && <div className="status">Error: {error}</div>}
      {loading ? (
        <div className="loading">Loading…</div>
      ) : (
        <div className="cards">
          {rows.map((r) => {
            const total = r.from_supplements + (r.from_food ?? 0)
            const pct = r.coverage_pct
            return (
              <div className="card" key={r.key} title={r.source}>
                <div className="label">{r.name}</div>
                <div className="value">
                  {pct === null ? '—' : `${pct}%`}
                  <small> of {r.rda} {r.unit}/day</small>
                </div>
                <div
                  style={{
                    height: 8, borderRadius: 4, background: 'var(--track, #e5e7eb)',
                    overflow: 'hidden', marginTop: 6,
                  }}
                >
                  <div
                    style={{
                      height: '100%', width: `${Math.min(pct ?? 0, 100)}%`,
                      background: (pct ?? 0) >= 100 ? '#16a34a' : '#3b82f6',
                    }}
                  />
                </div>
                <div className="sub">
                  {total} {r.unit} today
                  {' '}· supplements {r.from_supplements} {r.unit}
                  {' '}· food {r.from_food === null ? '—' : `${r.from_food} ${r.unit}`}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
