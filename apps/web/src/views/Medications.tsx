import { useCallback, useEffect, useMemo, useState } from 'react'
import { authClient } from '../lib/auth'
import { fmtAmount, fmtPct, isoDate, trimNum } from '../lib/format'

/**
 * Supplements & Meds — 0.11.0 §3 (intake v2).
 * Shows what was TAKEN, nothing else: week (default) / month grid, a
 * collapsed Cabinet, a bulk Log-intake modal, dose popovers and a
 * composition modal with full Edit. No regimen/adherence language
 * anywhere: no expected slots, no "not logged", no zeros.
 */

interface MedItem {
  id: string
  kind: string
  name: string
  short_name?: string | null
  brand?: string | null
  ingredients_json?: Array<Record<string, unknown>> | null
  dose_amount?: number | null
  dose_unit?: string | null
  unit_label?: string | null
  status: string
  notes?: string | null
}

interface IntakeDose {
  dose_id: string
  item_id: string
  name: string
  short_name?: string | null
  kind: string
  color_idx?: number | null
  amount: number
  unit_label?: string | null
  taken_at: string
  note?: string | null
}

interface IntakeDay {
  date: string // YYYY-MM-DD
  doses: IntakeDose[]
}

interface IntakeItem {
  item_id: string
  name: string
  short_name?: string | null
  kind: string
  color_idx?: number | null
  days_taken: number
  total_amount: number
  unit_label?: string | null
  last_taken_at?: string | null
}

interface IntakeResponse {
  view: string
  start: string
  end: string
  tz?: string
  days: IntakeDay[]
  items: IntakeItem[]
}

interface CompositionRow {
  name: string
  nutrient_code?: string | null
  mapping?: string | null
  per_unit?: number | null
  unit?: string | null
  taken_total?: number | null
  avg_per_day_taken?: number | null
  ref?: number | null
  ref_kind?: string | null
  ul?: number | null
  /** % of the label's daily value (from the supplement facts panel). */
  pct_dv?: number | null
  per?: string | null
  serving_units?: number | null
  form?: string | null
  pct_ref?: number | null
  above_ul?: boolean
}

interface Composition {
  item?: {
    id?: string; name?: string; short_name?: string | null; kind?: string; brand?: string | null;
    dose_amount?: number | null; dose_unit?: string | null; unit_label?: string | null;
    ingredients_json?: Array<Record<string, unknown>> | null
  } | null
  item_id?: string
  name?: string
  kind?: string
  scope?: string
  taken?: { amount_units?: number | null; days_taken?: number | null; doses?: number | null } | null
  rows: CompositionRow[]
}

/** 12-char client truncation at a word boundary (server sends short_name; this is the fallback). */
function shortName(name: string, fallback?: string | null): string {
  if (fallback && fallback.trim()) return fallback
  if (name.length <= 12) return name
  const cut = name.slice(0, 12)
  const space = cut.lastIndexOf(' ')
  return (space > 3 ? cut.slice(0, space) : cut).trim()
}

function hashColorIdx(itemId: string): number {
  let h = 0
  for (let i = 0; i < itemId.length; i++) h = (h * 31 + itemId.charCodeAt(i)) >>> 0
  return h % 8
}

function colorVar(dose: IntakeDose): string {
  const idx = dose.color_idx ?? hashColorIdx(dose.item_id)
  return `var(--med-color-${((idx % 8) + 8) % 8})`
}

/* ---- date helpers (ISO week, Monday first) ------------------------------ */

function mondayOf(d: Date): Date {
  const date = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const day = (date.getDay() + 6) % 7 // Mon=0
  date.setDate(date.getDate() - day)
  return date
}

function addDays(d: Date, n: number): Date {
  const date = new Date(d)
  date.setDate(date.getDate() + n)
  return date
}

function weekDays(offset: number): Date[] {
  const mon = addDays(mondayOf(new Date()), offset * 7)
  return Array.from({ length: 7 }, (_, i) => addDays(mon, i))
}

function monthCells(offset: number): (Date | null)[] {
  const now = new Date()
  const first = new Date(now.getFullYear(), now.getMonth() + offset, 1)
  const lead = (first.getDay() + 6) % 7
  const daysIn = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate()
  const cells: (Date | null)[] = []
  for (let i = 0; i < lead; i++) cells.push(null)
  for (let i = 1; i <= daysIn; i++) cells.push(new Date(first.getFullYear(), first.getMonth(), i))
  while (cells.length % 7 !== 0) cells.push(null)
  return cells
}

const WEEKDAY = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

function dayLabel(d: Date): string {
  return `${WEEKDAY[(d.getDay() + 6) % 7]} ${d.getDate()}`
}

function periodLabel(view: 'week' | 'month', offset: number): string {
  if (view === 'week') {
    const days = weekDays(offset)
    const a = days[0]
    const b = days[6]
    const fmt = (d: Date, withYear: boolean) =>
      d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(withYear ? { year: 'numeric' as const } : {}) })
    return `${fmt(a, a.getFullYear() !== b.getFullYear())} – ${fmt(b, true)}`
  }
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth() + offset, 1).toLocaleDateString(undefined, {
    month: 'long', year: 'numeric',
  })
}

function takenAtLabel(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

function dateTimeLocalValue(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function errMsg(e: unknown): string {
  const m = (e as { message?: string })?.message ?? String(e)
  return m.includes('401') || m.includes('403') ? 'Session expired — please log in again.' : m
}

/* ---- component ------------------------------------------------------------ */

type ViewMode = 'week' | 'month'
type KindFilter = 'all' | 'supplement' | 'medication'

export default function Medications() {
  const [view, setView] = useState<ViewMode>('week')
  const [offset, setOffset] = useState(0)
  const [kind, setKind] = useState<KindFilter>('all')
  const [intake, setIntake] = useState<IntakeResponse | null>(null)
  const [items, setItems] = useState<MedItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [toastKey, setToastKey] = useState(0)

  // modals / popovers
  const [logOpen, setLogOpen] = useState(false)
  const [doseSel, setDoseSel] = useState<IntakeDose | null>(null)
  const [compSel, setCompSel] = useState<{
    itemId: string
    name: string
    kind: string
    scope: 'unit' | 'day' | 'period'
    date?: string
    start?: string
    end?: string
    header?: string
  } | null>(null)
  const [drawerDay, setDrawerDay] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const showToast = useCallback((msg: string) => {
    setToast(msg)
    setToastKey((k) => k + 1)
  }, [])

  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 4200)
    return () => clearTimeout(t)
  }, [toast, toastKey])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [intakeRes, itemsRes] = await Promise.all([
        authClient.api(`/medications/intake?view=${view}&offset=${offset}&kind=${kind}`),
        authClient.api('/medications?status=all').catch(() => ({ items: [] })),
      ])
      setIntake(intakeRes as IntakeResponse)
      setItems(itemsRes.items || [])
    } catch (e) {
      setIntake(null)
      setError(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [view, offset, kind])

  useEffect(() => { load() }, [load])

  const daysByDate = useMemo(() => {
    const map = new Map<string, IntakeDose[]>()
    for (const day of intake?.days ?? []) map.set(day.date, day.doses)
    return map
  }, [intake])

  async function archiveItem(item: MedItem) {
    setBusyId(item.id)
    try {
      await authClient.api(`/medications/${item.id}/stop`, { method: 'POST', body: JSON.stringify({}) })
      showToast(`Archived ${item.name}`)
      await load()
    } catch (e) {
      showToast(errMsg(e))
    } finally {
      setBusyId(null)
    }
  }

  async function unarchiveItem(item: MedItem) {
    setBusyId(item.id)
    try {
      try {
        await authClient.api(`/medications/${item.id}/unarchive`, { method: 'POST', body: JSON.stringify({}) })
      } catch (e) {
        // Older API exposes resume; fall back once.
        if ((e as { status?: number })?.status === 404) {
          await authClient.api(`/medications/${item.id}/resume`, { method: 'POST', body: JSON.stringify({}) })
        } else throw e
      }
      showToast(`Restored ${item.name} to cabinet`)
      await load()
    } catch (e) {
      showToast(errMsg(e))
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="meds-view meds-view--v2">
      <div className="meds-header">
        <h2>Supplements &amp; meds</h2>
        <button onClick={() => setLogOpen(true)}>+ Log intake</button>
      </div>

      <div className="meds-toolbar">
        <div className="tabs meds-tabs" role="tablist" aria-label="View">
          <button className={`tab ${view === 'week' ? 'active' : ''}`} role="tab" aria-selected={view === 'week'} onClick={() => setView('week')}>Week</button>
          <button className={`tab ${view === 'month' ? 'active' : ''}`} role="tab" aria-selected={view === 'month'} onClick={() => setView('month')}>Month</button>
        </div>
        {/* DateNavigator-style offset nav: 0 = current period, -1 = previous. */}
        <div className="date-navigator meds-nav">
          <button className="date-nav-btn" onClick={() => setOffset((o) => o - 1)} aria-label="Previous period">‹</button>
          <span className="date-nav-label">{periodLabel(view, offset)}{offset === 0 ? ' · This week' : ''}</span>
          <button className="date-nav-btn" onClick={() => setOffset((o) => o + 1)} aria-label="Next period">›</button>
        </div>
        <div className="meds-filter" role="group" aria-label="Filter by kind">
          {(['all', 'supplement', 'medication'] as KindFilter[]).map((k) => (
            <button key={k} className={kind === k ? '' : 'secondary'} onClick={() => setKind(k)}>
              {k === 'all' ? 'All' : k === 'supplement' ? 'Supplements' : 'Meds'}
            </button>
          ))}
        </div>
      </div>

      {toast && <div className="status meds-toast" role="status">{toast}</div>}
      {error && (
        <div className="error" role="alert">
          Could not load intake: {error}
          <button className="secondary" onClick={load} style={{ marginLeft: 8 }}>Retry</button>
        </div>
      )}

      {loading ? (
        <div className="loading">Loading…</div>
      ) : (
        <>
          {intake && view === 'week' && (
            <section className="section meds-grid-section" aria-label="Week intake">
              <div className="meds-week">
                {weekDays(offset).map((d) => {
                  const key = isoDate(d)
                  const doses = daysByDate.get(key) ?? []
                  const isToday = key === isoDate()
                  return (
                    <div key={key} className={`meds-day${isToday ? ' meds-day--today' : ''}`}>
                      <div className="meds-day-label">{dayLabel(d)}{isToday ? <span className="meds-today-tag">Today</span> : null}</div>
                      <div className="meds-day-chips">
                        {doses.length === 0 ? (
                          <span className="meds-empty-dot" aria-label="Nothing taken">·</span>
                        ) : (
                          groupDayChips(doses).map((c) => (
                            <button
                              key={c.key}
                              type="button"
                              className="meds-chip"
                              onClick={() => setCompSel({ itemId: c.first.item_id, name: c.first.name, kind: c.first.kind, scope: 'day', date: key })}
                              aria-label={`${c.first.name}, ${trimNum(c.total, 1)}${c.first.unit_label ? ` ${c.first.unit_label}` : ''}${c.count > 1 ? `, logged ${c.count} times` : ''}`}
                            >
                              <span aria-hidden="true">{c.first.kind === 'medication' ? '💊' : '🌿'}</span>{' '}
                              <span className="meds-chip-name">{shortName(c.first.name, c.first.short_name)}</span>
                              {(c.total !== 1 || c.count > 1) && <span className="meds-chip-x">×{trimNum(c.total, 1)}</span>}
                            </button>
                          ))
                        )}
                      </div>
                    </div>
                  )
                })}
              </div>
            </section>
          )}

          {intake && view === 'month' && (
            <section className="section meds-grid-section" aria-label="Month intake">
              <div className="meds-month-head" aria-hidden="true">
                {WEEKDAY.map((w) => <span key={w}>{w}</span>)}
              </div>
              <div className="meds-month">
                {monthCells(offset).map((d, i) => {
                  if (!d) return <div key={`x-${i}`} className="meds-cell meds-cell--blank" aria-hidden="true" />
                  const key = isoDate(d)
                  const doses = daysByDate.get(key) ?? []
                  const uniq: IntakeDose[] = []
                  const seen = new Set<string>()
                  for (const dose of doses) if (!seen.has(dose.item_id)) { seen.add(dose.item_id); uniq.push(dose) }
                  const isToday = key === isoDate()
                  return (
                    <button
                      key={key}
                      type="button"
                      className={`meds-cell${isToday ? ' meds-cell--today' : ''}`}
                      onClick={() => setDrawerDay(key)}
                      aria-label={`${dayLabel(d)}${doses.length ? `, ${uniq.length} item${uniq.length === 1 ? '' : 's'} taken` : ''}`}
                    >
                      <span className="meds-cell-day">{d.getDate()}</span>
                      {uniq.length > 0 && (
                        <span className="meds-dots">
                          {uniq.slice(0, 3).map((dose) => (
                            <span key={dose.item_id} className="meds-dot" style={{ background: colorVar(dose) }} />
                          ))}
                          {uniq.length > 3 && <span className="meds-more">+{uniq.length - 3}</span>}
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            </section>
          )}

          <Cabinet items={items} busyId={busyId} onArchive={archiveItem} onUnarchive={unarchiveItem} onDetail={(it) => setCompSel({ itemId: it.id, name: it.name, kind: it.kind, scope: 'unit' })} onChanged={load} showToast={showToast} />
        </>
      )}

      {logOpen && (
        <LogIntakeModal
          items={items}
          lastTaken={new Map((intake?.items ?? []).map((i) => [i.item_id, i.last_taken_at ?? null]))}
          onClose={() => setLogOpen(false)}
          onLogged={(n, when) => {
            showToast(`Logged ${n} item${n === 1 ? '' : 's'} for ${when}`)
            setLogOpen(false)
            load()
          }}
        />
      )}

      {doseSel && (
        <DosePopover dose={doseSel} onClose={() => setDoseSel(null)} onChanged={() => { setDoseSel(null); load() }} showToast={showToast} />
      )}

      {compSel && (
        <CompositionModal
          sel={compSel}
          onClose={() => setCompSel(null)}
          showToast={showToast}
          onEditDose={compSel.scope === 'day' && compSel.date ? () => {
            const dose = (daysByDate.get(compSel.date ?? '') ?? []).find((d) => d.item_id === compSel.itemId)
            if (dose) { setCompSel(null); setDoseSel(dose) }
            else showToast('No dose logged that day to edit')
          } : undefined}
          onChanged={load}
        />
      )}

      {drawerDay && intake && (
        <DayDrawer
          day={drawerDay}
          doses={daysByDate.get(drawerDay) ?? []}
          onClose={() => setDrawerDay(null)}
          onDose={(d) => setDoseSel(d)}
          onComposition={(d) => setCompSel({ itemId: d.item_id, name: d.name, kind: d.kind, scope: 'day', date: drawerDay })}
        />
      )}
    </div>
  )
}

/** Group one day's doses into one chip per item (summed amount). */
function groupDayChips(doses: IntakeDose[]) {
  const map = new Map<string, { key: string; first: IntakeDose; total: number; count: number }>()
  for (const d of doses) {
    const g = map.get(d.item_id)
    if (g) { g.total += d.amount; g.count += 1 }
    else map.set(d.item_id, { key: d.item_id, first: d, total: d.amount, count: 1 })
  }
  return [...map.values()]
}

/* ---- Cabinet -------------------------------------------------------------- */

function Cabinet({ items, busyId, onArchive, onUnarchive, onDetail, onChanged, showToast }: {
  items: MedItem[]
  busyId: string | null
  onArchive: (i: MedItem) => void
  onUnarchive: (i: MedItem) => void
  onDetail: (i: MedItem) => void
  onChanged: () => void
  showToast: (m: string) => void
}) {
  const active = items.filter((i) => i.status === 'active')
  const archived = items.filter((i) => i.status === 'stopped')
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<MedItem | null>(null)

  return (
    <section className="section meds-cabinet">
      <details className="meds-cabinet-details">
        <summary>▸ Cabinet ({active.length} items)</summary>
        <div className="meds-cabinet-body">
          {active.length === 0 && <p className="muted">No items in cabinet yet.</p>}
          {active.map((i) => (
            <div className="meds-cabinet-row" key={i.id}>
              <span className="meds-item-icon" aria-hidden="true">{i.kind === 'medication' ? '💊' : '🌿'}</span>
              <span className="meds-cabinet-main">
                <button type="button" className="link meds-cabinet-name" onClick={() => onDetail(i)}>{i.name}</button>
                <span className="meds-cabinet-meta">
                  {i.brand ?? '—'} · {i.unit_label ?? i.dose_unit ?? '—'} · {i.ingredients_json?.length ?? 0} components
                </span>
              </span>
              <span className="meds-cabinet-actions">
                <button className="secondary" disabled={busyId === i.id} onClick={() => setEditing(i)}>Edit</button>
                <button className="danger" disabled={busyId === i.id} onClick={() => onArchive(i)}>Archive</button>
              </span>
            </div>
          ))}
          {archived.length > 0 && (
            <div className="meds-archived">
              <div className="meds-archived-label">Archived ({archived.length})</div>
              {archived.map((i) => (
                <div className="meds-cabinet-row meds-cabinet-row--archived" key={i.id}>
                  <span className="meds-cabinet-main">
                    <span className="meds-cabinet-name">{i.name}</span>
                    <span className="meds-cabinet-meta">{i.brand ?? '—'}</span>
                  </span>
                  <span className="meds-cabinet-actions">
                    <button className="secondary" disabled={busyId === i.id} onClick={() => onUnarchive(i)}>Unarchive</button>
                  </span>
                </div>
              ))}
            </div>
          )}
          <button className="secondary" onClick={() => setAdding((v) => !v)}>{adding ? 'Cancel' : '+ Add to cabinet'}</button>
          {adding && <AddItemForm onDone={() => { setAdding(false); onChanged() }} showToast={showToast} />}
        </div>
      </details>
      {editing && <EditItemForm item={editing} onClose={() => setEditing(null)} onDone={() => { setEditing(null); onChanged() }} showToast={showToast} />}
    </section>
  )
}

function AddItemForm({ onDone, showToast }: { onDone: () => void; showToast: (m: string) => void }) {
  const [kind, setKind] = useState('supplement')
  const [name, setName] = useState('')
  const [brand, setBrand] = useState('')
  const [dose, setDose] = useState('1')
  const [unit, setUnit] = useState('capsule')
  const [ingredientsText, setIngredientsText] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  async function save() {
    setSaving(true)
    setFormError(null)
    try {
      const ingredients = ingredientsText
        .split('\n').map((l) => l.trim()).filter(Boolean)
        .map((line) => {
          const [n, amt, u] = line.split(',').map((s) => (s ?? '').trim())
          return { name: n, amount: Number(amt) || null, unit: u || null, nutrient_code: null, mapping: 'none' as const }
        })
      await authClient.api('/medications', {
        method: 'POST',
        body: JSON.stringify({
          kind, name, brand: brand || null,
          dose_amount: Number(dose) || null, dose_unit: unit,
          unit_label: unit, ingredients_json: ingredients.length ? ingredients : undefined,
        }),
      })
      showToast(`Added ${name} to cabinet`)
      onDone()
    } catch (e) {
      setFormError(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="form-card meds-form">
      <div className="meds-add-grid">
        <label>Kind
          <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: '100%' }}>
            <option value="supplement">Supplement</option>
            <option value="medication">Medication</option>
          </select>
        </label>
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Vitamin D3" style={{ width: '100%' }} /></label>
        <label>Brand<input value={brand} onChange={(e) => setBrand(e.target.value)} style={{ width: '100%' }} /></label>
        <label>Usual amount<input value={dose} onChange={(e) => setDose(e.target.value)} inputMode="decimal" style={{ width: '100%' }} /></label>
        <label>Unit<input value={unit} onChange={(e) => setUnit(e.target.value)} placeholder="capsule" style={{ width: '100%' }} /></label>
      </div>
      <label style={{ display: 'block', marginTop: 8, fontSize: 13, fontWeight: 600 }}>Components (one per line: name, amount, unit)
        <textarea value={ingredientsText} onChange={(e) => setIngredientsText(e.target.value)} rows={3} style={{ width: '100%', marginTop: 4 }} placeholder={'Vitamin D3, 25, mcg'} />
      </label>
      {formError && <div className="error" role="alert">{formError}</div>}
      <div className="form-actions" style={{ marginTop: 12 }}>
        <button onClick={save} disabled={!name || saving}>{saving ? 'Saving…' : 'Save'}</button>
      </div>
    </div>
  )
}

function EditItemForm({ item, onClose, onDone, showToast }: { item: MedItem; onClose: () => void; onDone: () => void; showToast: (m: string) => void }) {
  const [name, setName] = useState(item.name)
  const [shortName, setShortName] = useState(item.short_name ?? '')
  const [brand, setBrand] = useState(item.brand ?? '')
  const [unit, setUnit] = useState(item.unit_label ?? item.dose_unit ?? '')
  const [dose, setDose] = useState(String(item.dose_amount ?? 1))
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  async function save() {
    setSaving(true)
    setFormError(null)
    try {
      await authClient.api(`/medications/${item.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ name, short_name: shortName.trim() || null, brand: brand || null, unit_label: unit || null, dose_amount: Number(dose) || null }),
      })
      showToast(`Saved ${name}`)
      onDone()
    } catch (e) {
      setFormError(errMsg(e)) // keep the draft on screen — never silently lose edits
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup meds-detail" role="dialog" aria-modal="true" aria-label={`Edit ${item.name}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><h2>Edit item</h2><button className="modal-close" onClick={onClose} aria-label="Close">✕</button></div>
        <div className="meds-add-grid">
          <label>Name<input value={name} onChange={(e) => setName(e.target.value)} style={{ width: '100%' }} /></label>
          <label>Abbreviation (weekly chips)<input value={shortName} onChange={(e) => setShortName(e.target.value)} placeholder="e.g. Mg, MultiV" style={{ width: '100%' }} /></label>
          <label>Brand<input value={brand} onChange={(e) => setBrand(e.target.value)} style={{ width: '100%' }} /></label>
          <label>Usual amount<input value={dose} onChange={(e) => setDose(e.target.value)} inputMode="decimal" style={{ width: '100%' }} /></label>
          <label>Unit<input value={unit} onChange={(e) => setUnit(e.target.value)} style={{ width: '100%' }} /></label>
        </div>
        {formError && <div className="error" role="alert">{formError}</div>}
        <div className="form-actions" style={{ marginTop: 12 }}>
          <button onClick={save} disabled={!name || saving}>{saving ? 'Saving…' : 'Save'}</button>
          <button className="secondary" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

/* ---- Log intake modal ------------------------------------------------------ */

function LogIntakeModal({ items, lastTaken, onClose, onLogged }: {
  items: MedItem[]
  lastTaken: Map<string, string | null>
  onClose: () => void
  onLogged: (n: number, when: string) => void
}) {
  const cabinet = items.filter((i) => i.status === 'active')
  const sorted = useMemo(
    () => [...cabinet].sort((a, b) => (lastTaken.get(b.id) ?? '').localeCompare(lastTaken.get(a.id) ?? '')),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items],
  )
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState<Map<string, number>>(new Map())
  const [when, setWhen] = useState(dateTimeLocalValue(new Date()))
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const filtered = sorted.filter((i) => i.name.toLowerCase().includes(search.trim().toLowerCase()))

  function toggle(item: MedItem) {
    setSelected((prev) => {
      const next = new Map(prev)
      if (next.has(item.id)) next.delete(item.id)
      else next.set(item.id, item.dose_amount ?? 1)
      return next
    })
  }

  function setAmount(id: string, v: number) {
    setSelected((prev) => {
      const next = new Map(prev)
      if (Number.isFinite(v) && v > 0) next.set(id, v)
      return next
    })
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  async function save() {
    if (selected.size === 0) return
    setSaving(true)
    setFormError(null)
    try {
      const takenAt = when ? new Date(when).toISOString() : new Date().toISOString()
      const res = await authClient.api('/medications/intake', {
        method: 'POST',
        body: JSON.stringify({
          taken_at: takenAt,
          entries: [...selected.entries()].map(([item_id, amount]) => ({
            item_id, amount, note: note.trim() || undefined,
          })),
        }),
      })
      const n = Array.isArray(res?.doses) ? res.doses.length : selected.size
      const d = new Date(takenAt)
      const label = d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
      onLogged(n, label)
    } catch (e) {
      setFormError(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup meds-detail" role="dialog" aria-modal="true" aria-label="Log intake" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><h2>Log intake</h2><button className="modal-close" onClick={onClose} aria-label="Close">✕</button></div>
        <input
          type="search" value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search cabinet…" aria-label="Search cabinet" style={{ width: '100%', marginBottom: 8 }}
        />
        <div className="meds-log-list">
          {filtered.length === 0 && <p className="muted">No items match.</p>}
          {filtered.map((i) => {
            const isSel = selected.has(i.id)
            const amt = selected.get(i.id) ?? i.dose_amount ?? 1
            return (
              <div key={i.id} className={`meds-log-row${isSel ? ' meds-log-row--sel' : ''}`}>
                <label className="meds-log-check">
                  <input type="checkbox" checked={isSel} onChange={() => toggle(i)} />
                  <span aria-hidden="true">{i.kind === 'medication' ? '💊' : '🌿'}</span>
                  <span className="meds-log-name">{i.name}</span>
                </label>
                {isSel && (
                  <span className="meds-stepper">
                    <button type="button" aria-label={`Less ${i.name}`} onClick={() => setAmount(i.id, Math.max(0.5, amt - 0.5))}>−</button>
                    <input
                      type="number" min="0.5" step="0.5" value={amt} aria-label={`Amount of ${i.name}`}
                      onChange={(e) => setAmount(i.id, Number(e.target.value))}
                    />
                    <button type="button" aria-label={`More ${i.name}`} onClick={() => setAmount(i.id, amt + 0.5)}>+</button>
                    <span className="meds-stepper-unit">{i.unit_label ?? i.dose_unit ?? ''}</span>
                  </span>
                )}
              </div>
            )
          })}
        </div>
        <div className="meds-log-when">
          <label>When
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} style={{ marginLeft: 8 }} />
          </label>
          <button type="button" className="secondary" onClick={() => setWhen(dateTimeLocalValue(addDays(new Date(), -1)))}>Yesterday</button>
        </div>
        <label style={{ display: 'block', marginTop: 8, fontSize: 13, fontWeight: 600 }}>Note (optional)
          <input value={note} onChange={(e) => setNote(e.target.value)} style={{ width: '100%', marginTop: 4 }} placeholder="With breakfast…" />
        </label>
        {formError && <div className="error" role="alert">{formError}</div>}
        <div className="form-actions" style={{ marginTop: 12 }}>
          <button onClick={save} disabled={selected.size === 0 || saving}>{saving ? 'Saving…' : `Save (${selected.size})`}</button>
          <button className="secondary" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

/* ---- Dose popover ---------------------------------------------------------- */

function DosePopover({ dose, onClose, onChanged, showToast }: {
  dose: IntakeDose
  onClose: () => void
  onChanged: () => void
  showToast: (m: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [amount, setAmount] = useState(String(dose.amount))
  const [when, setWhen] = useState(dose.taken_at ? dateTimeLocalValue(new Date(dose.taken_at)) : '')
  const [note, setNote] = useState(dose.note ?? '')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  async function save() {
    setSaving(true)
    setFormError(null)
    try {
      await authClient.api(`/medications/doses/${dose.dose_id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          amount: Number(amount) || dose.amount,
          taken_at: when ? new Date(when).toISOString() : undefined,
          note: note || null,
        }),
      })
      showToast('Dose updated')
      onChanged()
    } catch (e) {
      setFormError(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  async function remove() {
    setSaving(true)
    setFormError(null)
    try {
      await authClient.api(`/medications/doses/${dose.dose_id}`, { method: 'DELETE' })
      showToast('Dose deleted')
      onChanged()
    } catch (e) {
      setFormError(errMsg(e))
      setSaving(false)
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup meds-dose-pop" role="dialog" aria-modal="true" aria-label={`${dose.name} dose`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{dose.kind === 'medication' ? '💊' : '🌿'} {dose.name}</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {editing ? (
          <>
            <label style={{ display: 'block', fontSize: 13, fontWeight: 600 }}>Amount ({dose.unit_label ?? ''})
              <input type="number" min="0.5" step="0.5" value={amount} onChange={(e) => setAmount(e.target.value)} style={{ width: '100%', marginTop: 4 }} />
            </label>
            <label style={{ display: 'block', marginTop: 8, fontSize: 13, fontWeight: 600 }}>When
              <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} style={{ width: '100%', marginTop: 4 }} />
            </label>
            <label style={{ display: 'block', marginTop: 8, fontSize: 13, fontWeight: 600 }}>Note
              <input value={note} onChange={(e) => setNote(e.target.value)} style={{ width: '100%', marginTop: 4 }} />
            </label>
          </>
        ) : (
          <>
            <p className="meds-detail-sub">{takenAtLabel(dose.taken_at)} · {trimNum(dose.amount, 1)}{dose.unit_label ? ` ${dose.unit_label}` : ''}</p>
            {dose.note && <p>{dose.note}</p>}
          </>
        )}
        {formError && <div className="error" role="alert">{formError}</div>}
        <div className="form-actions" style={{ marginTop: 12 }}>
          {editing ? (
            <>
              <button onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
              <button className="secondary" onClick={() => setEditing(false)}>Cancel</button>
            </>
          ) : (
            <>
              <button onClick={() => setEditing(true)}>Edit amount/time</button>
              <button className="danger" onClick={remove} disabled={saving}>Delete</button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/* ---- Day drawer (month view) ------------------------------------------------ */

function DayDrawer({ day, doses, onClose, onDose, onComposition }: {
  day: string
  doses: IntakeDose[]
  onClose: () => void
  onDose: (d: IntakeDose) => void
  onComposition: (d: IntakeDose) => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])
  const d = new Date(`${day}T12:00:00`)
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup meds-detail" role="dialog" aria-modal="true" aria-label={`Intake on ${day}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {doses.length === 0 ? (
          <p className="muted">Nothing taken this day.</p>
        ) : (
          <div className="meds-log-list">
            {doses.map((dose) => (
              <div key={dose.dose_id} className="meds-log-row">
                <span aria-hidden="true">{dose.kind === 'medication' ? '💊' : '🌿'}</span>
                <span className="meds-log-name">{dose.name}</span>
                <span className="meds-cabinet-meta">{takenAtLabel(dose.taken_at)} · {trimNum(dose.amount, 1)}{dose.unit_label ? ` ${dose.unit_label}` : ''}</span>
                <span className="meds-cabinet-actions">
                  <button className="secondary" onClick={() => onComposition(dose)}>Composition</button>
                  <button className="secondary" onClick={() => onDose(dose)}>Edit</button>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/* ---- Composition modal ------------------------------------------------------ */

interface CompSelection {
  itemId: string
  name: string
  kind: string
  scope: 'unit' | 'day' | 'period'
  date?: string
  start?: string
  end?: string
  header?: string
}

function CompositionModal({ sel, onClose, showToast, onEditDose, onChanged }: {
  sel: CompSelection
  onClose: () => void
  showToast: (m: string) => void
  onEditDose?: () => void
  onChanged?: () => void
}) {
  const [detail, setDetail] = useState<Composition | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({ scope: sel.scope })
      if (sel.date) params.set('date', sel.date)
      if (sel.start) params.set('start', sel.start)
      if (sel.end) params.set('end', sel.end)
      const res = await authClient.api(`/medications/${sel.itemId}/composition?${params.toString()}`)
      setDetail(normaliseComposition(res))
    } catch (e) {
      setError(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [sel])

  useEffect(() => { load() }, [load])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const itemName = detail?.item?.name ?? detail?.name ?? sel.name
  const itemBrand = detail?.item?.brand
  const isPeriod = sel.scope === 'period'
  const taken = detail?.taken

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-popup meds-detail" role="dialog" aria-modal="true" aria-label={`${itemName} composition`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-row">
            <span className="modal-icon" aria-hidden="true">{sel.kind === 'medication' ? '💊' : '🌿'}</span>
            <h2>{itemName}{itemBrand ? <span className="meds-item-brand"> · {itemBrand}</span> : null}</h2>
          </div>
          <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <p className="meds-detail-sub">
          {sel.scope === 'day' && sel.date
            ? `Taken: ${new Date(`${sel.date}T12:00:00`).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })}${taken?.amount_units ? ` · ${trimNum(taken.amount_units, 1)}${detail?.item?.unit_label ? ` ${detail.item.unit_label}` : ''}` : ''}`
            : isPeriod
              ? (sel.header ?? 'Taken this period')
              : 'Per unit'}
        </p>
        {loading ? (
          <div className="loading">Loading…</div>
        ) : error ? (
          <div className="error" role="alert">Could not load composition: {error}<button className="secondary" onClick={load} style={{ marginLeft: 8 }}>Retry</button></div>
        ) : detail && detail.rows.length > 0 ? (
          <div className="table-wrap meds-comp-wrap">
            <table className="data-table meds-comp">
              <thead>
                <tr>
                  <th>Component</th>
                  <th className="num">Per caps.</th>
                  {sel.scope !== 'unit' && <th className="num">Taken</th>}
                  {isPeriod && <th className="num">Avg / day taken</th>}
                  <th className="num">Ref/day</th>
                  <th className="num">% DV (label)</th>
                  <th className="num">% REF</th>
                </tr>
              </thead>
              <tbody>
                {detail.rows.map((r, idx) => {
                  const hasRef = r.nutrient_code !== null && r.nutrient_code !== undefined && r.ref !== null && r.ref !== undefined
                  const aboveUl = r.above_ul === true || (r.ul !== null && r.ul !== undefined && r.taken_total !== null && r.taken_total !== undefined && r.taken_total > r.ul)
                  return (
                    <tr key={idx}>
                      <td className="meds-comp-name">{r.name}</td>
                      <td className="num tabular">{fmtAmount(r.per_unit, r.unit)}</td>
                      {sel.scope !== 'unit' && <td className="num tabular">{fmtAmount(r.taken_total, r.unit)}</td>}
                      {isPeriod && <td className="num tabular">{fmtAmount(r.avg_per_day_taken, r.unit)}</td>}
                      <td className="num tabular">{hasRef ? fmtAmount(r.ref, r.unit) : '—'}</td>
                      <td className="num tabular">{r.pct_dv !== null && r.pct_dv !== undefined ? `${trimNum(r.pct_dv, 1)}%` : '—'}</td>
                      <td className="num tabular">
                        {hasRef ? fmtPct(r.pct_ref) : '—'}
                        {aboveUl && r.ul !== null && r.ul !== undefined && (
                          <span className="meds-ul"> UL {fmtAmount(r.ul, r.unit)}</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No composition recorded for this item.</p>
        )}
        <p className="meds-detail-note">“—” means no daily requirement is recorded for that component (e.g. glucosamine, boswellia) — not zero. % DV (label) is from the supplement facts panel; % REF is computed against the NASEM reference intake.</p>
        <div className="form-actions" style={{ marginTop: 12 }}>
          <button type="button" className="secondary" onClick={() => setEditing(true)}>Edit composition</button>
          {sel.scope === 'day' && onEditDose && (
            <button type="button" className="secondary" onClick={onEditDose}>Edit dose</button>
          )}
        </div>
        {editing && detail?.item && (
          <EditCompositionForm
            itemId={sel.itemId}
            item={detail.item}
            onClose={() => setEditing(false)}
            onSaved={async () => {
              setEditing(false)
              await load()
              onChanged?.()
            }}
            showToast={showToast}
          />
        )}
      </div>
    </div>
  )
}

/* ---- Edit composition popup -------------------------------------------------- */

interface EditRow {
  name: string
  amount: string
  unit: string
  nutrient_code: string
  per: string
  serving_units: string
  form: string
  mapping: string
  pct_dv: string
}

function toEditRow(e: Record<string, unknown>): EditRow {
  return {
    name: String(e.name ?? e.component ?? e.ingredient ?? ''),
    amount: e.amount != null ? String(e.amount) : '',
    unit: String(e.unit ?? ''),
    nutrient_code: String(e.nutrient_code ?? ''),
    per: String(e.per ?? 'unit'),
    serving_units: e.serving_units != null ? String(e.serving_units) : '1',
    form: String(e.form ?? ''),
    mapping: String(e.mapping ?? 'none'),
    pct_dv: e.pct_dv != null ? String(e.pct_dv) : '',
  }
}

/**
 * Full Edit for a medication item: item-level fields (name, short_name
 * abbreviation, brand, dose_amount, dose_unit, unit_label) plus every
 * ingredient row (name, amount, unit, nutrient_code, per, serving_units,
 * form, mapping, pct_dv label %). Saves via PATCH /medications/{id}.
 */
function EditCompositionForm({ itemId, item, onClose, onSaved, showToast }: {
  itemId: string
  item: NonNullable<Composition['item']>
  onClose: () => void
  onSaved: () => void | Promise<void>
  showToast: (m: string) => void
}) {
  const [name, setName] = useState(item.name ?? '')
  const [shortName, setShortName] = useState(item.short_name ?? '')
  const [brand, setBrand] = useState(item.brand ?? '')
  const [doseAmount, setDoseAmount] = useState(item.dose_amount != null ? String(item.dose_amount) : '')
  const [doseUnit, setDoseUnit] = useState(item.dose_unit ?? '')
  const [unitLabel, setUnitLabel] = useState(item.unit_label ?? '')
  const [rows, setRows] = useState<EditRow[]>(() => (item.ingredients_json ?? []).map(toEditRow))
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  function setRow(idx: number, f: keyof EditRow, v: string) {
    setRows((prev) => prev.map((r, i) => (i === idx ? { ...r, [f]: v } : r)))
  }

  function numOrNull(s: string): number | null {
    const t = s.trim()
    if (!t) return null
    const n = Number(t)
    return Number.isFinite(n) ? n : null
  }

  async function save() {
    if (!name.trim()) {
      setFormError('Name is required.')
      return
    }
    const bad = rows.findIndex((r) => !r.name.trim())
    if (bad >= 0) {
      setFormError(`Ingredient ${bad + 1} is missing a name — fill it or remove the row.`)
      return
    }
    setSaving(true)
    setFormError(null)
    try {
      await authClient.api(`/medications/${itemId}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: name.trim(),
          short_name: shortName.trim() || null,
          brand: brand.trim() || null,
          dose_amount: numOrNull(doseAmount),
          dose_unit: doseUnit.trim() || null,
          unit_label: unitLabel.trim() || null,
          ingredients_json: rows.map((r) => ({
            name: r.name.trim(),
            amount: numOrNull(r.amount),
            unit: r.unit.trim() || null,
            nutrient_code: r.nutrient_code.trim() || null,
            per: r.per === 'serving' ? 'serving' : 'unit',
            serving_units: numOrNull(r.serving_units) ?? 1,
            form: r.form === 'elemental' || r.form === 'compound' ? r.form : null,
            mapping: r.mapping === 'confirmed' || r.mapping === 'suggested' ? r.mapping : 'none',
            pct_dv: numOrNull(r.pct_dv),
          })),
        }),
      })
      showToast(`Saved ${name.trim()}`)
      await onSaved()
    } catch (e) {
      setFormError(errMsg(e)) // keep the draft on screen — never silently lose edits
    } finally {
      setSaving(false)
    }
  }

  const num = (label: string, v: string, onChange: (s: string) => void, ph?: string) => (
    <label>{label}
      <input type="number" inputMode="decimal" value={v} onChange={(e) => onChange(e.target.value)} placeholder={ph ?? ''} style={{ width: '100%' }} />
    </label>
  )
  const txt = (label: string, v: string, onChange: (s: string) => void, ph?: string) => (
    <label>{label}
      <input value={v} onChange={(e) => onChange(e.target.value)} placeholder={ph ?? ''} style={{ width: '100%' }} />
    </label>
  )

  return (
    <div className="modal-overlay" onClick={onClose} style={{ zIndex: 60 }}>
      <div className="modal-popup meds-detail" role="dialog" aria-modal="true" aria-label={`Edit ${item.name ?? 'composition'}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header"><h2>Edit composition</h2><button className="modal-close" onClick={onClose} aria-label="Close">✕</button></div>
        <div className="meds-add-grid">
          {txt('Name', name, setName)}
          {txt('Abbreviation (weekly chips)', shortName, setShortName, 'e.g. Mg, MultiV')}
          {txt('Brand', brand, setBrand)}
          {num('Usual amount', doseAmount, setDoseAmount)}
          {txt('Dose unit', doseUnit, setDoseUnit)}
          {txt('Unit label', unitLabel, setUnitLabel)}
        </div>
        <h3 style={{ margin: '16px 0 8px' }}>Components</h3>
        {rows.length === 0 && <p className="muted">No components recorded.</p>}
        {rows.map((r, idx) => (
          <div key={idx} className="meds-edit-row">
            <div className="meds-edit-row-head">
              <strong>#{idx + 1}</strong>
              <button type="button" className="danger" onClick={() => setRows((prev) => prev.filter((_, i) => i !== idx))}>Remove</button>
            </div>
            <div className="meds-add-grid">
              {txt('Name', r.name, (v) => setRow(idx, 'name', v))}
              {num('Amount', r.amount, (v) => setRow(idx, 'amount', v))}
              {txt('Unit', r.unit, (v) => setRow(idx, 'unit', v), 'mg, mcg…')}
              {txt('Nutrient code', r.nutrient_code, (v) => setRow(idx, 'nutrient_code', v), 'e.g. magnesium')}
              <label>Per
                <select value={r.per} onChange={(e) => setRow(idx, 'per', e.target.value)} style={{ width: '100%' }}>
                  <option value="unit">unit</option>
                  <option value="serving">serving</option>
                </select>
              </label>
              {num('Serving units', r.serving_units, (v) => setRow(idx, 'serving_units', v))}
              <label>Form
                <select value={r.form} onChange={(e) => setRow(idx, 'form', e.target.value)} style={{ width: '100%' }}>
                  <option value="">—</option>
                  <option value="elemental">elemental</option>
                  <option value="compound">compound</option>
                </select>
              </label>
              <label>Mapping
                <select value={r.mapping} onChange={(e) => setRow(idx, 'mapping', e.target.value)} style={{ width: '100%' }}>
                  <option value="confirmed">confirmed</option>
                  <option value="suggested">suggested</option>
                  <option value="none">none</option>
                </select>
              </label>
              {num('% DV (label)', r.pct_dv, (v) => setRow(idx, 'pct_dv', v), 'e.g. 115')}
            </div>
          </div>
        ))}
        <button type="button" className="secondary" style={{ marginTop: 8 }} onClick={() => setRows((prev) => [...prev, { name: '', amount: '', unit: '', nutrient_code: '', per: 'unit', serving_units: '1', form: '', mapping: 'none', pct_dv: '' }])}>
          + Add component
        </button>
        {formError && <div className="error" role="alert" style={{ marginTop: 8 }}>{formError}</div>}
        <div className="form-actions" style={{ marginTop: 12 }}>
          <button onClick={save} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
          <button className="secondary" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  )
}

/** Accept both the §3.4 shape and the legacy composition shape. */
function normaliseComposition(res: any): Composition {
  const rows: CompositionRow[] = Array.isArray(res?.rows)
    ? res.rows.map((r: any) => ({
      name: String(r.name ?? r.component ?? '—'),
      nutrient_code: r.nutrient_code ?? r.nutrient_key ?? null,
      mapping: r.mapping ?? r.raw?.mapping ?? null,
      per_unit: r.per_unit ?? r.per_capsule ?? null,
      unit: r.unit ?? null,
      taken_total: r.taken_total ?? r.per_dose_total ?? null,
      avg_per_day_taken: r.avg_per_day_taken ?? null,
      ref: r.ref ?? r.rda ?? null,
      ref_kind: r.ref_kind ?? null,
      ul: r.ul ?? null,
      pct_dv: r.pct_dv ?? r.raw?.pct_dv ?? null,
      per: r.per ?? r.raw?.per ?? null,
      serving_units: r.serving_units ?? r.raw?.serving_units ?? null,
      form: r.form ?? r.raw?.form ?? null,
      pct_ref: r.pct_ref ?? r.pct_per_capsule ?? null,
      above_ul: r.above_ul ?? null,
    }))
    : []
  return {
    item: res?.item ?? (res?.name ? { id: res.item_id, name: res.name, kind: res.kind } : null),
    item_id: res?.item_id,
    name: res?.name,
    kind: res?.kind,
    scope: res?.scope,
    taken: res?.taken ?? null,
    rows,
  }
}
