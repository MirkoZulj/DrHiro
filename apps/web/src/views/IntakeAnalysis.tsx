import { useCallback, useEffect, useMemo, useState } from 'react'
import { authClient } from '../lib/auth'
import { fmtAmount, fmtPct, isoDate } from '../lib/format'
import IntakeChart, { GROUP_LABELS, GROUP_ORDER, type IntakeNutrient, type IntakeView } from '../components/IntakeChart'

/** Intake analysis (§4.7): chart + controls + day drawer. Replaces NutritionCoverage.
 *  Needs GET /intake/overview + /intake/day (Jerry B1). Degrades to a clear
 *  "API pending" state until the backend lands — never fabricated numbers. */

interface Overview {
  view: string
  start: string
  end: string
  tz: string
  dri_version: string
  reference_profile: { sex: string; band: string; assumed: boolean }
  days: string[]
  nutrients: IntakeNutrient[]
  unknowns: { code: string; text: string; detail?: string | null }[]
  completeness_overall: number | null
}

interface DayDetail {
  date: string
  nutrients: (IntakeNutrient & { contributors?: { source: string; name: string; amount: number; unit?: string; food_id?: string | null; meal_item_id?: string }[] })[]
  meals?: { id: string; name?: string; eaten_at?: string; status?: string }[]
  doses?: { dose_id: string; item_id: string; name: string; kind: string; amount: number; unit_label?: string; taken_at?: string }[]
}

type Source = 'all' | 'food' | 'supplements'

function mondayOf(d: Date): Date {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const day = (x.getDay() + 6) % 7
  x.setDate(x.getDate() - day)
  return x
}
function periodLabel(view: IntakeView, offset: number): string {
  if (view === 'week') {
    const a = mondayOf(new Date()); a.setDate(a.getDate() + offset * 7)
    const b = new Date(a); b.setDate(b.getDate() + 6)
    const fmt = (d: Date, y: boolean) => d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(y ? { year: 'numeric' as const } : {}) })
    return `${fmt(a, a.getFullYear() !== b.getFullYear())} – ${fmt(b, true)}`
  }
  const n = new Date()
  return new Date(n.getFullYear(), n.getMonth() + offset, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
}
function fmtDayLong(iso: string): string {
  const d = new Date(`${iso}T00:00:00`)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })
}

export default function IntakeAnalysis() {
  const [view, setView] = useState<IntakeView>('week')
  const [offset, setOffset] = useState(0)
  const [source, setSource] = useState<Source>('all')
  const [sortByPct, setSortByPct] = useState(false)
  const [groups, setGroups] = useState<Record<string, boolean>>(() => Object.fromEntries(GROUP_ORDER.map((g) => [g, true])))
  const [data, setData] = useState<Overview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [apiPending, setApiPending] = useState(false)
  const [drawerDate, setDrawerDate] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null); setApiPending(false)
    try {
      const qs = new URLSearchParams({ view, offset: String(offset), source })
      const res = await authClient.api(`/intake/overview?${qs.toString()}`)
      setData(res as Overview)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      // 404/405 until Jerry's /intake lands — state it plainly, don't fake data.
      if (/404|405|not found|method not allowed/i.test(msg)) setApiPending(true)
      setError(msg)
    } finally { setLoading(false) }
  }, [view, offset, source])

  useEffect(() => { load() }, [load])

  const filtered = useMemo(() => {
    if (!data) return []
    let list = data.nutrients.filter((n) => groups[n.group] !== false)
    if (source === 'food') list = list.map((n) => ({ ...n, days: n.days.map((d) => ({ ...d, supp: 0, total: d.food, pct: n.ref ? (d.food != null ? (d.food / n.ref) * 100 : null) : null })) }))
    if (source === 'supplements') list = list.map((n) => ({ ...n, days: n.days.map((d) => ({ ...d, food: null, total: d.supp, pct: n.ref ? (d.supp / n.ref) * 100 : null })) }))
    return list
  }, [data, groups, source])

  const profile = data?.reference_profile
  const comp = data?.completeness_overall

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
        <h3 style={{ margin: '8px 0' }}>Intake analysis</h3>
        <span className="muted" style={{ fontSize: 12 }}>% of daily reference · food + supplements</span>
      </div>

      {/* controls */}
      <div className="form-actions" style={{ flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
        <button className={view === 'week' ? '' : 'secondary'} onClick={() => setView('week')}>Week</button>
        <button className={view === 'month' ? '' : 'secondary'} onClick={() => setView('month')}>Month</button>
        {/* DateNavigator-style period nav: 0 = current period */}
        <button className="date-nav-btn" onClick={() => setOffset((o) => o - 1)} aria-label="Previous period">‹</button>
        <span className="date-nav-label">{periodLabel(view, offset)}</span>
        <button className="date-nav-btn" onClick={() => setOffset((o) => o + 1)} disabled={offset >= 0} aria-label="Next period">›</button>
      </div>
      <div className="form-actions" style={{ flexWrap: 'wrap', gap: 6, marginBottom: 8 }}>
        {GROUP_ORDER.map((g) => (
          <button key={g} className={groups[g] ? '' : 'secondary'} style={{ fontSize: 11 }} onClick={() => setGroups((s) => ({ ...s, [g]: !s[g] }))}>
            {GROUP_LABELS[g]}
          </button>
        ))}
        <span className="muted" style={{ fontSize: 12, alignSelf: 'center' }}>Source:</span>
        {(['all', 'food', 'supplements'] as Source[]).map((s) => (
          <button key={s} className={source === s ? '' : 'secondary'} style={{ fontSize: 11 }} onClick={() => setSource(s)}>{s === 'all' ? 'All' : s === 'food' ? 'Food' : 'Supplements'}</button>
        ))}
        <button className={sortByPct ? '' : 'secondary'} style={{ fontSize: 11 }} onClick={() => setSortByPct((v) => !v)}>Sort by %</button>
      </div>

      {error && (
        <div className="status">
          {apiPending
            ? 'Intake API not yet available (/intake/overview pending backend B1). Chart will appear here once it lands — no data is fabricated in the meantime.'
            : `Could not load intake: ${error}`}
          <div style={{ marginTop: 6 }}><button className="secondary" onClick={load}>Retry</button></div>
        </div>
      )}
      {loading && <div className="loading">Loading…</div>}

      {!loading && data && (
        <>
          <div className="section">
            <div className="form-card" style={{ padding: 8 }}>
              <IntakeChart nutrients={filtered} days={data.days} view={view} sortByPct={sortByPct} onSelect={(date) => setDrawerDate(date)} />
              {view === 'month' && <p className="muted" style={{ fontSize: 12, margin: '6px 4px 0' }}>Average of {data.days.length} days with data · dots = per-day values · whisker = min–max. Click a bar for day detail.</p>}
              {view === 'week' && <p className="muted" style={{ fontSize: 12, margin: '6px 4px 0' }}>Each nutrient: Mon–Sun bars (food 55% opacity, supplements solid). Hatched = incomplete food data (&lt;80%). Orange outline = above UL. Grey tick = no data. Click a bar for day detail.</p>}
            </div>
          </div>

          {/* below chart */}
          <div className="form-card" style={{ marginBottom: 12 }}>
            <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
              Reference: NASEM DRI, {profile ? `${profile.sex} ${profile.band}` : '—'} · v {data.dri_version || 'nasem-2024.1'}
              {profile?.assumed ? ' · assumed profile — set sex and date of birth in Settings' : ''}
            </p>
            {comp != null && (
              <div style={{ marginBottom: 8 }}>
                <div className="label" style={{ fontSize: 12 }}>Food-data completeness (overall): {Math.round(comp * 100)}%</div>
                <div style={{ height: 8, borderRadius: 4, background: 'var(--color-surface-2, #f0f4f5)', overflow: 'hidden', marginTop: 4 }}>
                  <div style={{ height: '100%', width: `${Math.round(comp * 100)}%`, background: comp >= 0.8 ? 'var(--color-accent, #0d7c6b)' : 'var(--color-warn, #b26a00)' }} />
                </div>
              </div>
            )}
            <h4 style={{ margin: '10px 0 4px' }}>Unknowns ({data.unknowns?.length ?? 0})</h4>
            {(!data.unknowns || data.unknowns.length === 0) ? (
              <p className="muted" style={{ fontSize: 12 }}>Nothing flagged — all foods, units, minerals and mappings resolved.</p>
            ) : (
              <ul className="muted" style={{ fontSize: 12, paddingLeft: 18, margin: '4px 0' }}>
                {data.unknowns.map((u, i) => (
                  <li key={i}><strong>{u.code}</strong> — {u.text}{u.detail ? ` (${u.detail})` : ''}</li>
                ))}
              </ul>
            )}
            <details style={{ marginTop: 10 }}>
              <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>Show as table</summary>
              <div style={{ overflowX: 'auto', marginTop: 6 }}>
                <table>
                  <thead>
                    <tr><th>Nutrient</th>{data.days.map((d) => <th key={d}>{d.slice(8)}</th>)}<th>Avg %</th></tr>
                  </thead>
                  <tbody>
                    {filtered.map((n) => (
                      <tr key={n.code}>
                        <td>{n.label}</td>
                        {n.days.map((d) => <td key={d.date} className="num tabular">{d.pct == null ? '—' : fmtPct(d.pct)}</td>)}
                        <td className="num tabular">{n.summary?.avg_pct != null ? fmtPct(n.summary.avg_pct) : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          </div>
        </>
      )}

      {drawerDate && <DayDrawer date={drawerDate} onClose={() => setDrawerDate(null)} />}
    </div>
  )
}

/* ---- day drawer: /intake/day contributors + meals/doses + Match food ---- */
function DayDrawer({ date, onClose }: { date: string; onClose: () => void }) {
  const [detail, setDetail] = useState<DayDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [matchId, setMatchId] = useState('')
  const [matchMsg, setMatchMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    authClient.api(`/intake/day?date=${date}`)
      .then((d) => { if (live) setDetail(d as DayDetail) })
      .catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)) })
    return () => { live = false }
  }, [date])

  async function doMatch() {
    if (!matchId.trim()) return
    setBusy(true); setMatchMsg(null)
    try {
      // B2: PATCH micro-food match. Endpoint is Jerry B2; call best-effort and surface the result.
      await authClient.api(`/micro-foods/${encodeURIComponent(matchId.trim())}/match`, {
        method: 'PATCH',
        body: JSON.stringify({ date }),
      })
      setMatchMsg('Matched. Intake will recompute on next load.')
    } catch (e) {
      setMatchMsg(`Match failed: ${e instanceof Error ? e.message : String(e)} (B2 endpoint may not be live yet)`)
    } finally { setBusy(false) }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup meds-detail" role="dialog" aria-modal="true" aria-label={`Intake on ${date}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{fmtDayLong(date)}</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {error && <div className="status">Could not load day detail: {error}</div>}
        {!detail && !error && <div className="loading">Loading…</div>}
        {detail && (
          <>
            <table>
              <thead><tr><th>Nutrient</th><th>≈ Amount</th><th>% ref</th><th>Top contributors</th></tr></thead>
              <tbody>
                {(detail.nutrients || []).map((n) => (
                  <tr key={n.code}>
                    <td>{n.label}</td>
                    <td className="num tabular">{n.days?.[0]?.total != null ? `${n.days[0].food != null ? '≈ ' : ''}${fmtAmount(n.days[0].total, n.unit)}` : '—'}</td>
                    <td className="num tabular">{fmtPct(n.days?.[0]?.pct)}</td>
                    <td style={{ fontSize: 12 }}>
                      {(n.contributors || []).slice(0, 5).map((c, i) => (
                        <div key={i}>{c.source === 'food' ? '🍽' : '🌿'} {c.name} · {fmtAmount(c.amount, n.unit)}</div>
                      ))}
                      {(!n.contributors || n.contributors.length === 0) && <span className="muted">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {(detail.meals?.length || detail.doses?.length) ? (
              <div style={{ marginTop: 10, fontSize: 13 }}>
                <strong>Logged that day</strong>
                {(detail.meals || []).map((m) => <div key={m.id}>🍽 {m.name || 'Meal'} {m.eaten_at ? `· ${String(m.eaten_at).slice(11, 16)}` : ''}</div>)}
                {(detail.doses || []).map((d) => <div key={d.dose_id}>🌿 {d.name} ×{d.amount}{d.unit_label ? ` ${d.unit_label}` : ''}</div>)}
                <div style={{ marginTop: 6 }}><a href="/meals">Open Meals page</a></div>
              </div>
            ) : null}
            <div style={{ marginTop: 14, borderTop: '1px solid var(--color-border, #e2e7ea)', paddingTop: 10 }}>
              <strong style={{ fontSize: 13 }}>Match a food to its micro data</strong>
              <p className="muted" style={{ fontSize: 12, margin: '2px 0 6px' }}>Links an unmatched food so its micronutrients count toward intake (B2).</p>
              <div className="form-actions" style={{ gap: 6 }}>
                <input value={matchId} onChange={(e) => setMatchId(e.target.value)} placeholder="Food ID" style={{ flex: 1, minWidth: 140 }} />
                <button onClick={doMatch} disabled={busy || !matchId.trim()}>{busy ? 'Matching…' : 'Match food'}</button>
              </div>
              {matchMsg && <div className="status" style={{ marginTop: 6 }}>{matchMsg}</div>}
            </div>
          </>
        )}
        <p className="muted" style={{ fontSize: 11, marginTop: 10 }}>Day: {isoDate(new Date(`${date}T00:00:00`))}</p>
      </div>
    </div>
  )
}
