import { useCallback, useEffect, useRef, useState } from 'react'
import { authClient, API_BASE } from '../lib/auth'

/**
 * Medical Records — timeline of encounters extracted from documents sent to
 * drHiro on Telegram. Per approved design spec
 * (drhiro-records-mockup/DESIGN.md): vertical timeline with year separators,
 * encounter cards, per-document popup, full-screen scan lightbox.
 * All data comes from the stored text the bot transcribed — rendered
 * faithfully, never re-parsed or re-structured (data honesty rule).
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

/** First sentence of the notes, for the one-line card preview. */
function firstSentence(s: string | null): string {
  if (!s) return ''
  const m = s.match(/^(.+?[.!?])(\s|$)/)
  return (m ? m[1] : s).trim()
}

/** Lines that start a conclusion-style callout. */
const CONCLUSION_RE = /^(conclusion|impression|diagnosis|zaključak|dijagnoza)\s*[:\-—]?\s*/i

function splitContent(content: string): { paragraphs: string[]; conclusion: string | null } {
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
  const [encounters, setEncounters] = useState<EncounterSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await authClient.api('/medical/encounters')
      const list = Array.isArray(res) ? res : (res?.encounters ?? [])
      setEncounters(list)
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (loading) return <div className="loading">Loading medical records…</div>
  if (error) {
    return (
      <div className="records-view">
        <h2>Medical Records</h2>
        <p className="page-sub">Encounters extracted from documents you sent to drHiro.</p>
        <div className="error">{error}</div>
        <div style={{ textAlign: 'center', marginTop: 12 }}>
          <button onClick={load}>Retry</button>
        </div>
      </div>
    )
  }

  return (
    <div className="records-view">
      <h2>Medical Records</h2>
      <p className="page-sub">Encounters extracted from documents you sent to drHiro.</p>
      {encounters.length === 0 ? (
        <div className="empty">No medical records yet. Send a document photo to drHiro on Telegram.</div>
      ) : (
        <EncounterTimeline encounters={encounters} />
      )}
    </div>
  )
}

function EncounterTimeline({ encounters }: { encounters: EncounterSummary[] }) {
  // Contract returns the full list; sort newest first and group by year.
  const sorted = [...encounters].sort((a, b) =>
    (b.encounter_date || '').localeCompare(a.encounter_date || '')
  )

  const groups: { year: string; items: EncounterSummary[] }[] = []
  for (const enc of sorted) {
    const d = parseDate(enc.encounter_date)
    const year = d ? String(d.getFullYear()) : 'Unknown'
    const last = groups[groups.length - 1]
    if (last && last.year === year) last.items.push(enc)
    else groups.push({ year, items: [enc] })
  }

  return (
    <div className="records-timeline">
      {groups.map((g) => (
        <section key={g.year} className="records-year-group">
          <div className="records-year-label">{g.year}</div>
          <div className="records-year-items">
            {g.items.map((enc) => (
              <EncounterCard key={enc.id} encounter={enc} />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

function EncounterCard({ encounter }: { encounter: EncounterSummary }) {
  const [open, setOpen] = useState(false)
  const d = dayParts(encounter.encounter_date)
  // Scan presence: the summary contract doesn't include it, so start from an
  // optional `scan_count` the backend may add, and mark filled once the popup
  // reveals a record with source_image_path (persists for the session).
  const [hasScans, setHasScans] = useState(
    Number((encounter as any).scan_count ?? 0) > 0
  )
  const scanCount = Number((encounter as any).scan_count ?? 0)

  const subParts = [
    encounter.facility,
    `${encounter.record_count} document${encounter.record_count === 1 ? '' : 's'}`,
  ]
  if (hasScans) subParts.push(`${scanCount || ''} ${scanCount === 1 ? 'scan' : 'scans'}`.trim())

  return (
    <>
      <button type="button" className={`records-card${hasScans ? ' has-scans' : ''}`} onClick={() => setOpen(true)}>
        <div className="records-card-date">
          <div className="records-card-day">{d.day}</div>
          <div className="records-card-monyr">{d.mon} {d.year}</div>
        </div>
        <div className="records-card-main">
          <div className="records-card-title">{encounter.title}</div>
          <div className="records-card-sub">{subParts.filter(Boolean).join(' · ')}</div>
          <div className="records-card-notes">{firstSentence(encounter.notes)}</div>
        </div>
        <span className="records-card-chevron" aria-hidden="true">›</span>
      </button>
      {open && <EncounterPopup encounter={encounter} onClose={() => setOpen(false)} onScansSeen={() => setHasScans(true)} />}
    </>
  )
}

function EncounterPopup({ encounter, onClose, onScansSeen }: { encounter: EncounterSummary; onClose: () => void; onScansSeen: () => void }) {
  const [detail, setDetail] = useState<EncounterDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    authClient
      .api(`/medical/encounters/${encounter.id}`)
      .then((res) => { if (!cancelled) setDetail(res) })
      .catch((e) => { if (!cancelled) setError(e?.message ?? String(e)) })
    return () => { cancelled = true }
  }, [encounter.id])

  // Close on Esc. The lightbox handles its own Esc when open (it unmounts
  // on close and this listener is re-added), so this only fires while the
  // popup itself is the topmost layer.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const d = dayParts(encounter.encounter_date)

  return (
    <div className="modal-overlay records-overlay" onClick={onClose}>
      <div
        className="modal-popup records-popup"
        role="dialog"
        aria-modal="true"
        aria-label={encounter.title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="records-popup-header">
          <div className="records-popup-date">
            <div className="records-card-day">{d.day}</div>
            <div className="records-card-monyr">{d.mon} {d.year}</div>
          </div>
          <div className="records-popup-headline">
            <div className="records-card-title">{encounter.title}</div>
            <div className="records-card-sub">
              {[encounter.facility, `${encounter.record_count} document${encounter.record_count === 1 ? '' : 's'}`]
                .filter(Boolean)
                .join(' · ')}
            </div>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="records-popup-scroll" ref={scrollRef}>
          {error && <div className="error">{error}</div>}
          {!detail && !error && <div className="loading">Loading encounter…</div>}
          {detail && (
            <>
              {detail.notes && <div className="records-encounter-notes">{detail.notes}</div>}
              {(detail.records ?? []).length === 0 && (
                <div className="empty">No documents stored for this encounter.</div>
              )}
              {(detail.records ?? []).map((rec, i) => (
                <RecordSection key={rec.id} record={rec} index={i + 1} total={detail.records.length}
                  onUpdated={() => { setDetail(null); authClient.api(`/medical/encounters/${encounter.id}`).then(setDetail).catch(() => {}) }}
                  onScanFound={scanPathsOf(rec).length ? onScansSeen : undefined} />
              ))}
            </>
          )}
        </div>
      </div>
    </div>
  )
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
  if (!res.ok) throw new Error(`Scan load failed (${res.status})`)
  const blob = await res.blob()
  return URL.createObjectURL(blob)
}

function RecordSection({ record, index, total, onScanFound, onUpdated }: { record: RecordItem; index: number; total: number; onScanFound?: () => void; onUpdated?: () => void }) {
  const [scanIndex, setScanIndex] = useState<number | null>(null)
  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState(record.title ?? '')
  const [content, setContent] = useState(record.content ?? '')
  const [saving, setSaving] = useState(false)
  const paths = scanPathsOf(record)
  const hasScans = paths.length > 0
  useEffect(() => { if (onScanFound && hasScans) onScanFound() }, [onScanFound, hasScans])
  const { paragraphs, conclusion } = splitContent(record.content ?? '')

  return (
    <div className="records-doc">
      <div className="records-doc-header">
        <span className="records-doc-index">{String(index).padStart(2, '0')}</span>
        <span className="records-doc-title">{record.title}</span>
        <span className="badge records-doc-badge">{sourceLabel(record.source_type)}</span>
        <button type="button" className="records-edit-btn" onClick={() => { setTitle(record.title ?? ''); setContent(record.content ?? ''); setEditing(!editing) }}>{editing ? 'Cancel' : 'Edit title / text'}</button>
      </div>
      {editing && (
        <div className="records-edit-form">
          <label>Title<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          <label>Text<textarea value={content} onChange={(e) => setContent(e.target.value)} rows={6} /></label>
          <button type="button" disabled={saving} onClick={async () => {
            setSaving(true)
            try {
              await authClient.api(`/medical/records/${record.id}`, { method: 'PATCH', body: JSON.stringify({ title, content }) })
              setEditing(false); onUpdated?.()
            } catch (e: any) { alert(e?.message ?? String(e)) } finally { setSaving(false) }
          }}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
      )}
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
      {hasScans && (
        <div className="records-scan-gallery" aria-label={`Scans of ${record.title}`}>
          {paths.map((p, i) => (
            <ScanThumb
              key={`${p}-${i}`}
              recordId={record.id}
              index={i}
              label={`Scan ${i + 1} of ${paths.length} — ${record.title}`}
              onOpen={() => setScanIndex(i)}
            />
          ))}
        </div>
      )}
      {scanIndex !== null && (
        <ScanLightbox
          recordId={record.id}
          title={record.title}
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
      .catch(() => { /* leave placeholder; lightbox will surface the error */ })
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
      {url ? <img src={url} alt="" loading="lazy" /> : <span className="records-scan-thumb-ph">🖼 {index + 1}</span>}
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
      .catch((e) => { if (!cancelled) setError(e?.message ?? String(e)) })
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
