import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useLocation, useParams, Link } from 'react-router-dom'
import { authClient, API_BASE } from '../lib/auth'
import RecordsTimeline, { TimelineEvent } from '../components/RecordsTimeline'

/**
 * Medical Records — horizontal timeline (Part A, 0.12.0-b).
 * /records renders RecordsTimeline; /records/:encounterId#d-YYYY-MM-DD renders
 * the full encounter detail (all records fully expanded incl scans, grouped by
 * local recorded_at date). The old vertical year list was REMOVED (owner).
 * All data is the bot's faithful transcription — never re-parsed.
 */

interface EncounterSummary {
  id: string
  encounter_date: string
  facility: string | null
  title: string
  notes: string | null
  record_count: number
}

interface RecordItem {
  id: string
  title: string
  content: string
  source_type: string | null
  recorded_at: string | null
  source_image_path: string | null
  source_image_paths?: string[] | null
  scan_count?: number | null
  has_scans?: boolean | null
}

interface EncounterDetail extends EncounterSummary {
  records: RecordItem[]
}

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']

function parseDate(s: string): Date | null {
  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}

/** Friendly label for a document source type; falls back to the raw value. */
const SOURCE_LABELS: Record<string, string> = {
  telegram_photo: 'Photo',
  telegram_document: 'Document',
  manual: 'Manual',
}

function sourceLabel(st: string | null): string {
  if (!st) return 'Document'
  return SOURCE_LABELS[st] ?? st
}

function dayParts(s: string) {
  const d = parseDate(s)
  if (!d) return { day: '–', mon: '', year: '' }
  return {
    day: String(d.getDate()).padStart(2, '0'),
    mon: MONTHS[d.getMonth()],
    year: String(d.getFullYear()),
  }
}

/** Lines that start a conclusion-style callout. */
const CONCLUSION_RE = /^(conclusion|impression|diagnosis|zaključak|dijagnoza)\s*[:\-—]?\s*/i

export function splitContent(content: string): { paragraphs: string[]; conclusion: string | null } {
  // Split only on blank lines; single \n inside a paragraph is preserved
  // verbatim (trim is outer-only) and rendered via white-space: pre-wrap.
  const paragraphs: string[] = []
  let conclusion: string | null = null
  for (const para of content.split(/\n{2,}|\r\n\r\n/)) {
    const text = para.trim()
    if (!text) continue
    if (conclusion === null) {
      const m = text.match(CONCLUSION_RE)
      if (m) {
        conclusion = text.slice(m[0].length).trim()
        continue
      }
    }
    paragraphs.push(text)
  }
  return { paragraphs, conclusion }
}

export default function Records() {
  const { encounterId } = useParams<{ encounterId?: string }>()
  if (encounterId) return <EncounterDetailPage encounterId={encounterId} />
  return <TimelinePage />
}

function TimelinePage() {
  const navigate = useNavigate()

  const openEvent = useCallback(
    (ev: TimelineEvent) => {
      // Future dates clamp at today with a note (A6): navigate with hash, the
      // detail page resolves the nearest date and notes it.
      navigate(`/records/${ev.encounter_id}#d-${ev.date}`)
    },
    [navigate],
  )

  return (
    <div className="records-view">
      <h2>Medical Records</h2>
      <p className="page-sub">Encounters extracted from documents you sent to drHiro.</p>
      <RecordsTimeline
        onOpen={openEvent}
        restoreKey="records-timeline-scroll"
      />
    </div>
  )
}
function localDateOf(rec: RecordItem, fallback: string): string {
  // Group by LOCAL recorded_at date; fall back to encounter_date.
  if (rec.recorded_at) {
    const d = parseDate(rec.recorded_at)
    if (d) return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
  }
  const d = parseDate(fallback)
  return d ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` : fallback
}

function EncounterDetailPage({ encounterId }: { encounterId: string }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [detail, setDetail] = useState<EncounterDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [highlight, setHighlight] = useState<string | null>(null)
  // Encounter title/notes edit (main's Records feature, kept in the full-page
  // detail): inline form in the header, PATCH /medical/encounters/{id}.
  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState('')
  const [draftNotes, setDraftNotes] = useState('')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const fetchedRef = useRef(0)
  const load = useCallback(() => {
    setError(null)
    const id = ++fetchedRef.current
    authClient
      .api(`/medical/encounters/${encounterId}`)
      .then((res) => {
        if (id !== fetchedRef.current) return
        const enc = res?.encounter ?? res ?? {}
        setDetail({ ...enc, records: res?.records ?? enc.records ?? [] })
      })
      .catch((e) => {
        if (id !== fetchedRef.current) return
        // 404 when deleted while open -> toast + refetch affordance.
        const msg = e?.message ?? String(e)
        setError(/404|not found/i.test(msg) ? 'Encounter no longer exists (deleted).' : msg)
      })
  }, [encounterId])
  useEffect(() => { load() }, [load])

  function startEditing() {
    setDraftTitle(detail?.title ?? '')
    setDraftNotes(detail?.notes ?? '')
    setSaveError(null)
    setEditing(true)
  }

  async function saveEncounter() {
    if (!detail) return
    setSaving(true)
    setSaveError(null)
    try {
      const res = await authClient.api(`/medical/encounters/${encounterId}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: draftTitle.trim() || detail.title, notes: draftNotes || null }),
      })
      setDetail((prev) =>
        prev
          ? { ...prev, title: res?.title ?? draftTitle, notes: res?.notes ?? draftNotes }
          : prev,
      )
      setEditing(false)
    } catch (e: any) {
      // Surface the failure and KEEP the draft text — never silently lose it.
      setSaveError(e?.message ?? String(e))
    } finally {
      setSaving(false)
    }
  }

  // Anchor #d-YYYY-MM-DD (?date= equivalent): scroll + highlight 2s.
  useEffect(() => {
    if (!detail) return
    const hash = location.hash || ''
    const m = hash.match(/^#d-(\d{4}-\d{2}-\d{2})/)
    const dateId = m ? m[1] : null
    if (!dateId) return
    const groups = groupByDate(detail)
    let target = dateId
    let msg: string | null = null
    if (!groups.some((g) => g.date === dateId)) {
      // No exact date -> nearest + note.
      if (groups.length === 0) return
      const t = new Date(dateId + 'T00:00:00').getTime()
      let best = groups[0]
      let bestDiff = Infinity
      for (const g of groups) {
        const diff = Math.abs(new Date(g.date + 'T00:00:00').getTime() - t)
        if (diff < bestDiff) { bestDiff = diff; best = g }
      }
      target = best.date
      msg = `No records dated ${dateId} — showing nearest ${best.date}.`
    }
    setNote(msg)
    requestAnimationFrame(() => {
      const el = document.getElementById(`d-${target}`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' })
        el.setAttribute('aria-current', 'true')
        setHighlight(target)
        setTimeout(() => { setHighlight(null); el.removeAttribute('aria-current') }, 2000)
      }
    })
  }, [detail, location.hash])

  if (error) {
    return (
      <div className="records-view">
        <h2>Medical Records</h2>
        <div className="error" role="alert">{error}</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" onClick={load}>Retry</button>
          <Link className="btn" to="/records">← Back to timeline</Link>
        </div>
      </div>
    )
  }
  if (!detail) return <div className="loading">Loading encounter…</div>

  const d = dayParts(detail.encounter_date)
  const groups = groupByDate(detail)

  return (
    <div className="records-view">
      <div className="records-detail-header">
        <Link className="rt-back" to="/records" onClick={() => navigate(-1)}>← Timeline</Link>
        <div className="records-popup-date">
          <div className="records-card-day">{d.day}</div>
          <div className="records-card-monyr">{d.mon} {d.year}</div>
        </div>
        <div className="records-popup-headline">
          {editing ? (
            <div className="records-edit-form">
              <label>
                Title
                <input value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} aria-label="Encounter title" />
              </label>
              <label>
                Notes (text)
                <textarea value={draftNotes} onChange={(e) => setDraftNotes(e.target.value)} rows={3} aria-label="Encounter notes" />
              </label>
            </div>
          ) : (
            <>
              <div className="records-card-title">{detail.title}</div>
              <div className="records-card-sub">
                {[detail.facility, `${detail.record_count} document${detail.record_count === 1 ? '' : 's'}`]
                  .filter(Boolean)
                  .join(' · ')}
              </div>
            </>
          )}
        </div>
        <div className="records-edit-actions">
          {editing ? (
            <>
              <button type="button" onClick={saveEncounter} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
              <button type="button" className="secondary" onClick={() => { setEditing(false); setSaveError(null) }}>Cancel</button>
              {saveError && <span className="error records-edit-error" role="alert">Save failed: {saveError}</span>}
            </>
          ) : (
            <button type="button" className="secondary" onClick={startEditing}>Edit title / text</button>
          )}
        </div>
      </div>
      {note && <p className="note" role="note">{note}</p>}
      {detail.notes && <div className="records-encounter-notes">{detail.notes}</div>}
      {groups.length === 0 && (
        <div className="empty">No documents stored for this encounter.</div>
      )}
      {groups.map((g) => (
        <section
          key={g.date}
          id={`d-${g.date}`}
          className={highlight === g.date ? 'records-date-group highlighted' : 'records-date-group'}
          aria-label={`Records dated ${g.date}`}
        >
          <h3 className="records-date-heading">{g.date}</h3>
          {g.records.map((rec, i) => (
            <RecordSection key={rec.id} record={rec} index={i + 1} total={g.records.length}
              onUpdated={(updated) =>
                setDetail((prev) =>
                  prev ? { ...prev, records: prev.records.map((r) => (r.id === updated.id ? updated : r)) } : prev,
                )
              } />
          ))}
        </section>
      ))}
    </div>
  )
}

function groupByDate(detail: EncounterDetail): { date: string; records: RecordItem[] }[] {
  const map = new Map<string, RecordItem[]>()
  for (const rec of detail.records ?? []) {
    const date = localDateOf(rec, detail.encounter_date)
    map.set(date, [...(map.get(date) ?? []), rec])
  }
  return [...map.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, records]) => ({ date, records }))
}

/** All scan paths for a record: every entry of source_image_paths, falling
 *  back to the singular source_image_path for old records. */
function scanPathsOf(record: RecordItem): string[] {
  if (Array.isArray(record.source_image_paths) && record.source_image_paths.length > 0) {
    return record.source_image_paths.filter(Boolean)
  }
  return record.source_image_path ? [record.source_image_path] : []
}

/** Authenticated fetch of one scan image (by index into the record's
 *  source_image_paths) -> blob URL. */
async function fetchScanBlobUrl(recordId: string, index: number): Promise<string> {
  const token = authClient.token()
  const res = await fetch(`${API_BASE}/medical/scan/${recordId}?index=${index}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) {
    const err = new Error(`Scan load failed (${res.status})`) as Error & { status?: number }
    err.status = res.status
    throw err
  }
  const blob = await res.blob()
  return URL.createObjectURL(blob)
}

function isScanMissingError(e: unknown): boolean {
  const err = e as { status?: number; message?: string } | null
  return err?.status === 404 || /scan image missing|scan load failed \(404\)/i.test(err?.message ?? String(e ?? ''))
}

function RecordSection({ record, index, total, onScanFound, onUpdated }: { record: RecordItem; index: number; total: number; onScanFound?: () => void; onUpdated: (r: RecordItem) => void }) {
  const [scanIndex, setScanIndex] = useState<number | null>(null)
  const [current, setCurrent] = useState(record)
  useEffect(() => { setCurrent(record) }, [record])
  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState(record.title ?? '')
  const [draftContent, setDraftContent] = useState(record.content ?? '')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const paths = scanPathsOf(current)
  // Backend has_scans/scan_count already only count files that resolve on
  // disk; when present, trust it over the raw path list so a record whose
  // scan file is missing shows no gallery. Absent (legacy/tools payloads),
  // fall back to the path list and let ScanThumb handle a failed fetch.
  const hasScans = current.has_scans != null ? Boolean(current.has_scans) : paths.length > 0
  useEffect(() => { if (onScanFound && hasScans) onScanFound() }, [onScanFound, hasScans])
  const { paragraphs, conclusion } = splitContent(current.content ?? '')

  async function saveRecord() {
    setSaving(true)
    setSaveError(null)
    try {
      const res = await authClient.api(`/medical/records/${current.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: draftTitle.trim() || current.title, content: draftContent }),
      })
      const updated: RecordItem = {
        ...current,
        title: res?.title ?? draftTitle,
        content: res?.content ?? draftContent,
      }
      setCurrent(updated)
      onUpdated(updated)
      setEditing(false)
    } catch (e: any) {
      // Surface the failure and KEEP the draft text — never silently lose it.
      setSaveError(e?.message ?? String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="records-doc">
      <div className="records-doc-header">
        <span className="records-doc-index">{String(index).padStart(2, '0')}</span>
        <span className="records-doc-title">{current.title}</span>
        <span className="badge records-doc-badge">{sourceLabel(current.source_type)}</span>
        {!editing && (
          <button type="button" className="secondary records-edit-btn"
            onClick={() => { setDraftTitle(current.title ?? ''); setDraftContent(current.content ?? ''); setSaveError(null); setEditing(true) }}>
            Edit
          </button>
        )}
      </div>
      {editing ? (
        <div className="records-edit-form records-doc-body">
          <label>
            Title
            <input value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} aria-label="Record title" />
          </label>
          <label>
            Text
            <textarea value={draftContent} onChange={(e) => setDraftContent(e.target.value)} rows={8} aria-label="Record text" />
          </label>
          <div className="records-edit-actions">
            <button type="button" onClick={saveRecord} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
            <button type="button" className="secondary" onClick={() => { setEditing(false); setSaveError(null) }}>Cancel</button>
            {saveError && <span className="error records-edit-error" role="alert">Save failed: {saveError}</span>}
          </div>
        </div>
      ) : (
      <div className="records-doc-body">
        {paragraphs.map((p, i) => (
          <p key={i}>{p}</p>
        ))}
        {conclusion !== null && (
          <div className="records-conclusion">
            <div className="records-conclusion-label">Conclusion</div>
            <p>{conclusion}</p>
          </div>
        )}
      </div>
      )}
      {hasScans && (
        <div className="records-scan-gallery" aria-label={`Scans of ${current.title}`}>
          {paths.map((p, i) => (
            <ScanThumb
              key={`${p}-${i}`}
              recordId={current.id}
              index={i}
              label={`Scan ${i + 1} of ${paths.length} — ${current.title}`}
              onOpen={() => setScanIndex(i)}
            />
          ))}
        </div>
      )}
      {scanIndex !== null && (
        <ScanLightbox
          recordId={current.id}
          title={current.title}
          total={paths.length}
          initialIndex={scanIndex}
          onClose={() => setScanIndex(null)}
        />
      )}
      {index < total && <div className="records-doc-divider" role="presentation" />}
    </div>
  )
}

/** One thumbnail in the scan gallery; fetches its image lazily on visibility. */
function ScanThumb({ recordId, index, label, onOpen }: { recordId: string; index: number; label: string; onOpen: () => void }) {
  const [url, setUrl] = useState<string | null>(null)
  const [unavailable, setUnavailable] = useState(false)
  const [visible, setVisible] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const el = btnRef.current
    if (!el) return
    const obs = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) { setVisible(true); obs.disconnect() }
    }, { rootMargin: '200px' })
    obs.observe(el)
    return () => obs.disconnect()
  }, [])

  useEffect(() => {
    if (!visible) return
    let objectUrl: string | null = null
    let cancelled = false
    fetchScanBlobUrl(recordId, index)
      .then((u) => { if (cancelled) { URL.revokeObjectURL(u) } else { objectUrl = u; setUrl(u) } })
      .catch(() => { if (!cancelled) setUnavailable(true) })
    return () => { cancelled = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [visible, recordId, index])

  return (
    <button
      type="button"
      ref={btnRef}
      className="records-scan-thumb records-scan-pill"
      onClick={onOpen}
      aria-label={label}
    >
      {url ? <img src={url} alt="" loading="lazy" /> : unavailable ? <span className="records-scan-thumb-ph records-scan-thumb-ph--unavailable">Scan unavailable</span> : <span className="records-scan-thumb-ph">🖼 {index + 1}</span>}
    </button>
  )
}

/** Full-screen dark lightbox with prev/next paging through all scans. */
function ScanLightbox({ recordId, title, total, initialIndex, onClose }: { recordId: string; title: string; total: number; initialIndex: number; onClose: () => void }) {
  const [index, setIndex] = useState(initialIndex)
  const [url, setUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const clamped = Math.min(Math.max(index, 0), Math.max(total - 1, 0))
  const go = useCallback((d: number) => setIndex((i) => Math.min(Math.max(i + d, 0), Math.max(total - 1, 0))), [total])

  useEffect(() => {
    let objectUrl: string | null = null
    let cancelled = false
    setUrl(null)
    setError(null)
    fetchScanBlobUrl(recordId, clamped)
      .then((u) => { if (cancelled) URL.revokeObjectURL(u); else { objectUrl = u; setUrl(u) } })
      .catch((e) => { if (!cancelled) setError(isScanMissingError(e) ? 'Scan image missing' : (e?.message ?? String(e))) })
    return () => { cancelled = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [recordId, clamped])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onClose() }
      else if (e.key === 'ArrowLeft') { e.stopPropagation(); go(-1) }
      else if (e.key === 'ArrowRight') { e.stopPropagation(); go(1) }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [onClose, go])

  return (
    <div className="records-lightbox" onClick={onClose}>
      <div className="records-lightbox-bar">
        <span className="records-lightbox-title">{title}</span>
        <span className="records-lightbox-count">Scan {clamped + 1} of {total}</span>
        <button type="button" className="records-lightbox-close" onClick={onClose} aria-label="Close scan">✕</button>
      </div>
      <div className="records-lightbox-body" onClick={(e) => e.stopPropagation()}>
        {total > 1 && (
          <button type="button" className="records-lightbox-nav records-lightbox-prev" onClick={() => go(-1)} disabled={clamped === 0} aria-label="Previous scan">‹</button>
        )}
        {error && <div className="records-lightbox-error">{error}</div>}
        {!url && !error && <div className="records-lightbox-loading">Loading scan…</div>}
        {url && <img src={url} alt={`${title} — scan ${clamped + 1} of ${total}`} />}
        {total > 1 && (
          <button type="button" className="records-lightbox-nav records-lightbox-next" onClick={() => go(1)} disabled={clamped >= total - 1} aria-label="Next scan">›</button>
        )}
      </div>
    </div>
  )
}
