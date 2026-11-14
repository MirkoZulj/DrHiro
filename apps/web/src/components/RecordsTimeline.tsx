import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { authClient } from '../lib/auth'

/**
 * RecordsTimeline — horizontal medical-records timeline (Part A, 0.12.0-b).
 *
 * Inline SVG + DOM scroll container, no deps. Data from GET /medical/timeline
 * (same auth as /medical/encounters). One dot per event (one per distinct
 * date, spec A2 — same-date records share an event via record_ids[]);
 * series rendered as a soft hull with per-dot stems. Dots are
 * keyboard-focusable buttons.
 */

export type TimelineKind = 'lab' | 'visit' | 'rx' | 'imaging' | 'surgery' | 'hospital' | 'other'

export interface TimelineEvent {
  id: string
  encounter_id: string
  date: string // ISO YYYY-MM-DD
  kind: string
  title: string
  facility: string | null
  record_ids: string[]
  series_id: string | null
  series_size: number
}

export interface TimelinePayload {
  birth_date: string | null
  today: string
  timezone: string
  events: TimelineEvent[]
  count: number
}

const KIND_LABEL: Record<string, string> = {
  lab: 'Lab', visit: 'Visit', rx: 'Prescription', imaging: 'Imaging',
  surgery: 'Surgery', hospital: 'Hospital', other: 'Other',
}

export const KIND_COLORS: Record<string, string> = {
  lab: 'var(--kind-lab, #2b6cb0)',
  visit: 'var(--kind-visit, #0d7c6b)',
  rx: 'var(--kind-rx, #7c3aed)',
  imaging: 'var(--kind-imaging, #0369a1)',
  surgery: 'var(--kind-surgery, #b91c1c)',
  hospital: 'var(--kind-hospital, #d97706)',
  other: 'var(--kind-other, #6b7280)',
}

/**
 * Timeline v4 (owner feedback 2026-11-09): full range birth → today is
 * always present and scrollable (keep All); the viewport WINDOW is 2-3
 * years at a time (default zoom 3y, initial view = last 3y, scroll left
 * to explore earlier). At 2-3y zoom the x-axis labels Mar / Jun / Sep on
 * each year so the user can tell which part of the year events fell in.
 * Time-window + Today buttons: transparent bg, green text, green fill +
 * white text on hover/active (see index.css .rt-zoom/.rt-today).
 */
export const CHART_H_DESKTOP = 320
export const CHART_H_MOBILE = 280
/**
 * v5 (owner 2026-11-09): dots live in the CENTER band (40%-60% of chart
 * height) — vertically centered, spread by hash jitter so they are not
 * in a straight row, with room above for an always-above popup.
 */
export const CENTER_BAND_TOP = 0.40
export const CENTER_BAND_BOTTOM = 0.60
/** @deprecated v5 alias kept for any external import — use CENTER_BAND_*. */
export const UPPER_BAND_TOP = CENTER_BAND_TOP
export const UPPER_BAND_BOTTOM = CENTER_BAND_BOTTOM

const DAY_MS = 86_400_000
export const ZOOMS = ['1y', '3y', '5y', '10y', 'All'] as const
export type Zoom = (typeof ZOOMS)[number]
/** v3 (kept in v4): default view shows 3 years ("up to 3 years in the frame"). */
export const DEFAULT_ZOOM = '3y' as const
/**
 * v4: quarter-year axis labels — Mar / Jun / Sep at Mar 1 / Jun 1 / Sep 1 boundaries.
 * month is the 0-based Date month (2=Mar, 5=Jun, 8=Sep).
 */
export const QUARTER_MONTHS = [
  { month: 2, label: 'Mar' },
  { month: 5, label: 'Jun' },
  { month: 8, label: 'Sep' },
] as const
/** Minimum px between quarter labels (~91 days apart) to draw them; below
 *  this (e.g. All zoom on a lifetime span) they are thinned out entirely. */
export const MIN_QUARTER_PX = 28
/** Cap on chart pixel width — browsers cap SVG widths well below the raw
 *  full-range width at 1y zoom over a ~47y lifespan (~57k px).
 * @deprecated zoomPxPerDay no longer caps by this: exact viewport spans
 *  (owner 2026-11-09) take precedence, so at 1y the chart can exceed
 *  32000px. Kept for external import compatibility. */
export const MAX_CHART_W = 32000
/** Viewport span each N-year zoom must show, in days (never clamped). */
export const ZOOM_SPAN_DAYS: Record<Exclude<Zoom, 'All'>, number> = {
  '1y': 365, '3y': 3 * 365, '5y': 5 * 365, '10y': 10 * 365,
}
/** Floor for 'All' only: below this the full span stays scrollable. */
export const MIN_ALL_PX_PER_DAY = 0.05

/**
 * v4 quarter (Mar/Jun/Sep) ticks for the current window: one per quarter
 * month per year inside [winStart, winEnd], only when the quarter spacing
 * (91 days * pxPerDay) is wide enough to label without crowding.
 * At 3y zoom on desktop (~1.1 px/day → ~100px/quarter) all 3 labels per
 * year show; at All zoom with a small pxPerDay they thin to none.
 * Pure — exported for tests.
 */
export function quarterMonthTicks(
  winStart: Date,
  winEnd: Date,
  xFor: (d: Date) => number,
  pxPerDay: number,
  chartW: number,
): { x: number; label: string; month: number; year: number }[] {
  if (91 * pxPerDay < MIN_QUARTER_PX) return []
  const out: { x: number; label: string; month: number; year: number }[] = []
  for (let y = winStart.getFullYear(); y <= winEnd.getFullYear(); y++) {
    for (const q of QUARTER_MONTHS) {
      const d = new Date(y, q.month, 1)
      if (d < winStart || d > winEnd) continue
      const x = xFor(d)
      if (x >= 0 && x <= chartW) out.push({ x, label: q.label, month: q.month, year: y })
    }
  }
  return out
}

/**
 * v4 scale, fixed 2026-11-09 (owner: "10y shows 4y, All shows 20y, no
 * change 1y/3y"). Two corruptions made every zoom wrong:
 *  1. The MAX_CHART_W cap (32000/span ≈ 1.83 px/day over a ~48y span)
 *     bound BOTH 1y and 3y on wide screens → identical pxPerDay → no
 *     visible change between them.
 *  2. `vw` was window.innerWidth while the timeline lives in a
 *     max-width:1180px .app column, so the visible span was
 *     clientWidth/innerWidth (~0.44×) of the intended span.
 * Fix: N-year zooms are EXACT — pxPerDay = viewportWidth / (years*365),
 * no cap, no clamp (chartW may exceed MAX_CHART_W at 1y). 'All' fits the
 * full span into the viewport width, floored at MIN_ALL_PX_PER_DAY so a
 * huge span stays scrollable instead of vanishing. Callers must pass the
 * SCROLLER's clientWidth, not window.innerWidth. Pure — exported for tests.
 */
export function zoomPxPerDay(vw: number, zoom: Zoom, spanDays: number): number {
  const span = Math.max(365, spanDays)
  const width = Math.max(1, vw)
  if (zoom === 'All') return Math.max(MIN_ALL_PX_PER_DAY, width / span)
  return width / ZOOM_SPAN_DAYS[zoom]
}

function parseISO(s: string): Date | null {
  const d = new Date(s + 'T00:00:00')
  return isNaN(d.getTime()) ? null : d
}

/**
 * Minimum horizontal gap (px) between the DISPLAYED dots of one series.
 * Spec A2: one event per distinct date (same-date records share one event
 * via record_ids[] — there is deliberately no per-record dot). Distinct
 * dates days apart collapse to the same pixel at lifetime zooms
 * (pxPerDay can floor at 0.5), so series dots are spread to this gap
 * visually only; the underlying date/tooltip/detail anchor is unchanged.
 */
export const MIN_SERIES_GAP_PX = 12

/** Minimum centre distance (px) below which dots truly overlap. */
export const MIN_DOT_SEPARATION_PX = 8

/**
 * Deterministic pseudo-random jitter in [0, 1): FNV-1a hash of the id.
 * Stable across renders/zooms so dots don't jump around when re-rendering.
 */
export function hashJitter(id: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return ((h >>> 0) % 10000) / 10000
}

/**
 * Dot centre Y for an event: deterministic hash jitter inside the center
 * band (40%-60% of chart height). Distinct heights per id, not a straight
 * row, and always with >=100px of headroom on desktop (center band
 * 128-192px in a 320px frame) for the always-above popup.
 * Pure — exported for tests.
 */
export function dotYFor(id: string, chartH: number): number {
  const lo = chartH * CENTER_BAND_TOP
  const hi = chartH * CENTER_BAND_BOTTOM
  return Math.round((lo + hashJitter(id) * (hi - lo)) * 10) / 10
}

/**
 * v5 tooltip (owner 2026-11-09): the popup is ALWAYS above its dot —
 * the above/below flip is removed, because with dots in the center band
 * (40%-60%) an above card always fits and a straight vertical leader
 * can connect dot to popup. The earlier flip put the line going up
 * while the label sat below, which read as broken.
 * Card: pointer-events:none (.rt-card) so it never blocks clicks,
 * offset TOOLTIP_GAP_PX above the dot, horizontally centered on the dot
 * (left = dotX - 114, half the 228px card width) so the card's bottom
 * arrow and the SVG leader line (x = dotX, straight up) meet.
 */
export const TOOLTIP_GAP_PX = 20
/** Conservative rendered height of .rt-card (date + 2-line title + sub). */
export const TOOLTIP_CARD_EST_H = 80
/** Half the .rt-card width (228px) — centers the card on its dot. */
export const TOOLTIP_CARD_HALF_W = 114
/**
 * Card position for a dot at (dotX, dotY). Pure — exported for tests.
 * Always above: cardBottom (= top + EST_H) <= dotY - TOOLTIP_GAP_PX,
 * top >= 2, and the leader line from (dotX, dotY) straight up to
 * (dotX, dotY - TOOLTIP_GAP_PX) meets the card's bottom edge.
 */
export function tooltipCardPos(
  dotX: number,
  dotY: number,
  chartW: number,
): { left: number; top: number; placement: 'above' } {
  const top = Math.max(2, dotY - TOOLTIP_GAP_PX - TOOLTIP_CARD_EST_H)
  const left = Math.max(4, Math.min(dotX - TOOLTIP_CARD_HALF_W, chartW - 232))
  return { left, top, placement: 'above' as const }
}

export const TOP_LANE = 4

/**
 * Display x per event: xFor(date), with each series' dots spread to at
 * least MIN_SERIES_GAP_PX apart (sorted by date, nudged right). Non-series
 * events keep their true x. Pure — exported for tests.
 */
export function seriesDisplayX(
  events: TimelineEvent[],
  xFor: (d: Date) => number,
): Map<string, number> {
  const out = new Map<string, number>()
  for (const ev of events) out.set(ev.id, xFor(parseISO(ev.date)!))
  const bySeries = new Map<string, TimelineEvent[]>()
  for (const ev of events) {
    if (!ev.series_id) continue
    bySeries.set(ev.series_id, [...(bySeries.get(ev.series_id) ?? []), ev])
  }
  for (const evs of bySeries.values()) {
    const sorted = [...evs].sort((a, b) => a.date.localeCompare(b.date))
    let prev = -Infinity
    for (const ev of sorted) {
      const x = Math.max(out.get(ev.id)!, prev + MIN_SERIES_GAP_PX)
      // First dot of a series keeps its true x (prev=-Infinity).
      out.set(ev.id, prev === -Infinity ? out.get(ev.id)! : x)
      prev = out.get(ev.id)!
    }
  }
  return out
}

/**
 * Collision lanes above the baseline: dots whose display x is <12px from
 * the previous dot step up one lane, capped at TOP_LANE. Same-lane dots
 * are therefore >=12px apart by construction unless the cap is hit.
 * Pure — exported for tests.
 */
export function assignLanes(items: { id: string; x: number }[]): Map<string, number> {
  const sorted = [...items].sort((a, b) => a.x - b.x)
  const map = new Map<string, number>()
  let lastX = -Infinity
  let lane = 0
  for (const it of sorted) {
    if (it.x - lastX < 12) lane = Math.min(lane + 1, TOP_LANE)
    else lane = 0
    map.set(it.id, lane)
    lastX = it.x
  }
  return map
}

/**
 * "+n" bubble groups: only dots that STILL overlap after lane assignment —
 * centre distance < MIN_DOT_SEPARATION_PX AND same lane (i.e. lanes
 * exhausted at the TOP_LANE cap). Distinct-date series dots stepped to
 * different lanes are never bubbled, however close their dates are.
 * Returns map eventId -> bubble group (only for groups of >=2).
 * Pure — exported for tests.
 */
export function bubbleGroups(
  items: { id: string; x: number; y: number; lane: number }[],
): Map<string, { x: number; y: number; evs: string[] }> {
  interface Group { x: number; y: number; lane: number; evs: string[] }
  const groups: Group[] = []
  for (const it of items) {
    let g = groups.find(
      (gr) => Math.hypot(it.x - gr.x, it.y - gr.y) < MIN_DOT_SEPARATION_PX && gr.lane === it.lane,
    )
    if (!g) {
      g = { x: it.x, y: it.y, lane: it.lane, evs: [] }
      groups.push(g)
    }
    g.evs.push(it.id)
  }
  const out = new Map<string, { x: number; y: number; evs: string[] }>()
  for (const g of groups) if (g.evs.length > 1) for (const id of g.evs) out.set(id, g)
  return out
}

interface Props {
  onOpen: (ev: TimelineEvent) => void
  restoreKey?: string
}

export default function RecordsTimeline({ onOpen, restoreKey }: Props) {
  const [payload, setPayload] = useState<TimelinePayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [zoom, setZoom] = useState<(typeof ZOOMS)[number]>(DEFAULT_ZOOM)
  const [hovered, setHovered] = useState<string | null>(null)
  const [focused, setFocused] = useState<string | null>(null)
  const [focusedIdx, setFocusedIdx] = useState<number>(-1)
  const scrollerRef = useRef<HTMLDivElement>(null)
  // Viewport width = the SCROLLER's clientWidth, NOT window.innerWidth.
  // The timeline sits in a max-width:1180px .app column inside a sidebar
  // shell, so innerWidth overstates the visible width (owner's wide
  // screen: ~2560 vs ~1114 → every zoom showed ~0.44× its span).
  // Initial value is only a pre-measure fallback; the layout effect below
  // replaces it with the real clientWidth on mount/payload-load/resize.
  const [vw, setVw] = useState(() => typeof window !== 'undefined' ? window.innerWidth : 800)
  const [scrollLeft, setScrollLeft] = useState(0)

  const load = useCallback(async () => {
    try {
      const res = await authClient.api('/medical/timeline')
      setPayload(res as TimelinePayload)
    } catch (e: any) {
      setError(e?.message ?? String(e))
    }
  }, [])
  useEffect(() => { load() }, [load])
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const measure = () => {
      const w = el.clientWidth
      if (w > 0) setVw(w)
    }
    measure()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    ro?.observe(el)
    window.addEventListener('resize', measure)
    return () => { ro?.disconnect(); window.removeEventListener('resize', measure) }
    // Re-run when payload arrives: the scroller only mounts after load.
  }, [payload])

  const events = payload?.events ?? []
  const birth = payload?.birth_date ? parseISO(payload.birth_date) : null
  const today = (payload?.today ? parseISO(payload.today) : null) ?? new Date()
  const earliest = (events.length ? parseISO(events[0].date) : null) ?? today

  // A6: no DOB -> earliest event -1y (or today-1y when empty).
  const start0 = birth ?? (() => {
    const base = earliest ?? today
    const d = new Date(base)
    d.setFullYear(d.getFullYear() - 1)
    return d
  })()
  const end0 = (() => {
    const d = new Date(today)
    d.setFullYear(d.getFullYear() + 1)
    return d
  })()

  // v4: the data range is ALWAYS the full lifespan birth → today(+1y
  // breathing room, as in v3 'All'), scrollable at every zoom. The zoom
  // only sets the viewport WINDOW via pxPerDay (zoomPxPerDay): default 3y
  // shows the last ~3 years and scrolling left explores earlier years
  // back to birth.
  const winStart = start0
  const winEnd = end0

  const spanDays = Math.max(365, Math.round((winEnd.getTime() - winStart.getTime()) / DAY_MS))
  const pxPerDay = zoomPxPerDay(vw, zoom, spanDays)
  const chartW = Math.round(spanDays * pxPerDay)
  // v3 frame kept: 320px desktop / 280px mobile; v5 dots in center band.
  // v4: baseline raised 18px to fit a month-label row below the year
  // labels (year +18, Mar/Jun/Sep +31, age +44).
  const chartH = vw < 640 ? CHART_H_MOBILE : CHART_H_DESKTOP
  const baselineY = chartH - 54
  const xFor = (d: Date) => Math.round(((d.getTime() - winStart.getTime()) / DAY_MS) * pxPerDay)
  /** Display x per event (series dots spread — see seriesDisplayX). */
  const displayX = seriesDisplayX(events, xFor)
  const xOf = (ev: TimelineEvent) => displayX.get(ev.id) ?? xFor(parseISO(ev.date)!)

  // Scroll to today on first layout + restore scroll position (A4 Back).
  useLayoutEffect(() => {
    const el = scrollerRef.current
    if (!el || !payload) return
    const key = restoreKey ?? 'timeline-scroll'
    const saved = sessionStorage.getItem(key)
    if (saved != null) {
      el.scrollLeft = Number(saved) || 0
    } else if (today) {
      el.scrollLeft = Math.max(0, xFor(today) - el.clientWidth / 2)
    }
    setScrollLeft(el.scrollLeft)
    const onScroll = () => { sessionStorage.setItem(key, String(el.scrollLeft)); setScrollLeft(el.scrollLeft) }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payload])

  const scrollToToday = useCallback(() => {
    const el = scrollerRef.current
    if (!el || !today) return
    el.scrollTo({ left: Math.max(0, xFor(today) - el.clientWidth / 2), behavior: 'smooth' })
  }, [today, winStart, pxPerDay])

  // v4: changing zoom re-centres on today (initial view = last N years);
  // the full birth → today range stays scrollable from there. 'All' fits
  // the whole range, so it pins to the start (birth) instead. Skipped on
  // mount so the A4 Back scroll-restore above wins.
  const firstZoom = useRef(true)
  useEffect(() => {
    if (firstZoom.current) { firstZoom.current = false; return }
    if (zoom === 'All') scrollerRef.current?.scrollTo({ left: 0, behavior: 'smooth' })
    else scrollToToday()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zoom])

  // Drag-to-pan (mouse / pen). Touch keeps native pan via touch-action.
  const dragRef = useRef<{ pointerId: number; startX: number; startLeft: number } | null>(null)
  const [dragging, setDragging] = useState(false)

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = scrollerRef.current
    if (!el || e.pointerType === 'touch') return
    if ((e.target as HTMLElement).closest('button, a')) return
    dragRef.current = { pointerId: e.pointerId, startX: e.clientX, startLeft: el.scrollLeft }
    setDragging(true)
    try { el.setPointerCapture?.(e.pointerId) } catch { /* ignore non-active pointer ids */ }
  }, [])

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    const el = scrollerRef.current
    const d = dragRef.current
    if (!el || !d || d.pointerId !== e.pointerId) return
    el.scrollLeft = d.startLeft - (e.clientX - d.startX)
  }, [])

  const endDrag = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId === e.pointerId) dragRef.current = null
    setDragging(false)
  }, [])

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    const el = scrollerRef.current
    if (!el) return
    const step = Math.max(80, el.clientWidth / 3)
    if (e.key === 'ArrowLeft') { el.scrollLeft -= step; e.preventDefault() }
    else if (e.key === 'ArrowRight') { el.scrollLeft += step; e.preventDefault() }
  }, [])

  // Wheel: Ctrl/Cmd = pinch zoom (existing); otherwise scroll the strip
  // horizontally (vertical wheel included) so a plain mouse can pan it.
  useEffect(() => {
    const el = scrollerRef.current
    if (!el) return
    const onWheelNative = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault()
        const rect = el.getBoundingClientRect()
        const cx = e.clientX - rect.left + el.scrollLeft
        const factor = e.deltaY < 0 ? 1.1 : 0.9
        const nextW = el.scrollWidth * factor
        if (nextW < vw) return // block <1y span
        const wrap = el.firstElementChild as HTMLElement | null
        if (!wrap) return
        const prevScale = Number(wrap.dataset.scale ?? '1')
        const nextScale = prevScale * factor
        wrap.style.transform = `scaleX(${nextScale})`
        wrap.style.transformOrigin = '0 0'
        wrap.dataset.scale = String(nextScale)
        el.scrollLeft = cx * factor
        return
      }
      e.preventDefault()
      const delta = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY
      el.scrollLeft += delta
    }
    el.addEventListener('wheel', onWheelNative, { passive: false })
    return () => el.removeEventListener('wheel', onWheelNative)
  }, [payload, vw])

  // Collision lanes, kept only as the bubble-grouping key (v3: dot heights
  // no longer come from lanes). Series dots are spread (seriesDisplayX) so
  // each distinct date keeps its own dot+stem.
  const lanes = assignLanes(events.map((ev) => ({ id: ev.id, x: xOf(ev) })))

  /** Final dot centre: deterministic jitter inside the center band
   *  (dotYFor / hashJitter) — no lane stacking at shared levels. The lane
   *  map is still passed to bubbleGroups so bubble semantics are unchanged:
   *  only same-lane dots <8px apart (lanes exhausted) can bubble. */
  const dotY = (ev: TimelineEvent) => dotYFor(ev.id, chartH)

  // "+n" bubbles only for dots still overlapping after lanes (same lane +
  // centre distance <8px, i.e. lanes exhausted) — never for series dots
  // that merely share a rounded x (old bug: exact-x grouping bubbled them
  // and suppressed their stems, reading as a single dot).
  const bubbles = bubbleGroups(
    events.map((ev) => ({ id: ev.id, x: xOf(ev), y: dotY(ev), lane: lanes.get(ev.id) ?? 0 })),
  )

  const series = (() => {
    const m = new Map<string, TimelineEvent[]>()
    for (const ev of events) if (ev.series_id) m.set(ev.series_id, [...(m.get(ev.series_id) ?? []), ev])
    return m
  })()

  const tooltipEv = hovered ?? focused
  const tooltip = tooltipEv ? events.find((e) => e.id === tooltipEv) : null

  if (error) return <div className="error">{error}</div>
  if (!payload) return <div className="loading">Loading timeline…</div>

  // Ticks. Year-begin is ALWAYS gridlined (every Jan 1) so the owner can see
  // where each year begins at any zoom; decades get a stronger gridline.
  // Labels: every year is labelled when <=5 years are in view (v3 default
  // 3y); wider spans (All, 47y) thin labels so they don't crowd, while the
  // Jan 1 gridlines remain.
  const spanYears = (winEnd.getTime() - winStart.getTime()) / (365.25 * DAY_MS)
  const yearPx = 365 * pxPerDay
  const labelEvery = spanYears <= 5 ? 1 : Math.max(1, Math.ceil(48 / yearPx))
  const yearTicks: { x: number; label: string; decade: boolean; showLabel: boolean }[] = []
  for (let y = winStart.getFullYear(); y <= winEnd.getFullYear(); y++) {
    const x = xFor(new Date(y, 0, 1))
    if (x >= 0 && x <= chartW) {
      yearTicks.push({
        x, label: String(y), decade: y % 10 === 0,
        showLabel: (y - winStart.getFullYear()) % labelEvery === 0 || y % 10 === 0,
      })
    }
  }
  const monthTicks: { x: number }[] = []
  if (pxPerDay >= 2) {
    for (let d = new Date(winStart); d <= winEnd; d.setMonth(d.getMonth() + 1)) {
      const x = xFor(d)
      if (x >= 0 && x <= chartW) monthTicks.push({ x })
    }
  }
  // v4: Mar / Jun / Sep labels below the year labels (thinned by
  // quarterMonthTicks when pxPerDay is too small, e.g. All zoom).
  const quarterTicks = quarterMonthTicks(winStart, winEnd, xFor, pxPerDay, chartW)
  const showAge = pxPerDay >= 0.5
  const ageRows: { x: number; label: string }[] = []
  if (showAge && birth) {
    const nowDate = today
    for (let a = 0; a <= 110; a++) {
      const d = new Date(birth)
      d.setFullYear(d.getFullYear() + a)
      if (d > nowDate) break
      const x = xFor(d)
      if (x >= 0 && x <= chartW) ageRows.push({ x, label: `${a}` })
    }
  }

  const HINT = 'Add your date of birth in Settings for an accurate age scale.'

  return (
    <div className="records-timeline-wrap">
      <div className="records-timeline-header" role="toolbar" aria-label="Timeline zoom">
        <div className="rt-zoom-group" role="group" aria-label="Zoom">
          {ZOOMS.map((z) => (
            <button
              key={z}
              type="button"
              className={zoom === z ? 'rt-zoom active' : 'rt-zoom'}
              aria-pressed={zoom === z}
              onClick={() => setZoom(z)}
            >
              {z}
            </button>
          ))}
        </div>
        <button type="button" className="rt-today" onClick={scrollToToday}>Today</button>
        <ul className="rt-legend" aria-label="Legend">
          {Object.keys(KIND_LABEL).map((k) => (
            <li key={k}>
              <span className="rt-legend-dot" style={{ background: KIND_COLORS[k] }} />
              {KIND_LABEL[k]}
            </li>
          ))}
        </ul>
      </div>

      {!birth && (
        <p className="rt-hint" role="note">{HINT}</p>
      )}

      <div
        className={dragging ? 'records-timeline-scroller dragging' : 'records-timeline-scroller'}
        ref={scrollerRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onKeyDown={onKeyDown}
        tabIndex={0}
        aria-label="Medical records timeline"
      >
        <div className="rt-chart" style={{ width: chartW, height: chartH }}>
          <svg
            width={chartW}
            height={chartH}
            role="group"
            aria-label="Timeline chart"
            className="rt-svg"
          >
            {/* thin baseline (1px) */}
            <line x1={0} y1={baselineY} x2={chartW} y2={baselineY} className="rt-baseline" />

            {/* year-begin gridlines: light vertical at every Jan 1, labelled
                below the baseline at every zoom; decades stronger */}
            {yearTicks.map((t) => (
              <g key={t.label}>
                <line x1={t.x} y1={8} x2={t.x} y2={baselineY} className={t.decade ? 'rt-year-grid decade' : 'rt-year-grid'} />
                <line x1={t.x} y1={baselineY} x2={t.x} y2={baselineY + 6} className="rt-tick" />
                {t.showLabel && (
                  <text x={t.x} y={baselineY + 18} className={t.decade ? 'rt-tick-label year decade' : 'rt-tick-label year'}>{t.label}</text>
                )}
              </g>
            ))}
            {/* month ticks (dense zoom only): tiny mark below baseline */}
            {monthTicks.map((t, i) => (
              <line key={i} x1={t.x} y1={baselineY} x2={t.x} y2={baselineY + 3} className="rt-tick month" />
            ))}

            {/* v4 quarter labels: Mar / Jun / Sep at Mar 1 / Jun 1 / Sep 1,
                small tick + text below the year labels (thinned at All) */}
            {quarterTicks.map((t) => (
              <g key={`${t.year}-${t.month}`}>
                <line x1={t.x} y1={baselineY} x2={t.x} y2={baselineY + 6} className="rt-tick quarter" />
                <text x={t.x} y={baselineY + 31} className="rt-tick-label month">{t.label}</text>
              </g>
            ))}

            {/* Born / Today markers */}
            {birth && (
              <g>
                <line x1={xFor(birth)} y1={8} x2={xFor(birth)} y2={baselineY} className="rt-marker born" />
                <text x={xFor(birth)} y={14} className="rt-marker-label">Born</text>
              </g>
            )}
            {today && (
              <g>
                <line x1={xFor(today)} y1={8} x2={xFor(today)} y2={baselineY} className="rt-marker today" />
                <text x={xFor(today)} y={14} className="rt-marker-label">Today</text>
              </g>
            )}

            {/* age row (below year + month labels) */}
            {ageRows.map((a) => (
              <text key={a.label} x={a.x} y={baselineY + 44} className="rt-tick-label age">{a.label}</text>
            ))}

            {/* series hulls: soft band above the baseline only */}
            {[...series.entries()].map(([sid, evs]) => {
              const pts = evs.map((e) => ({ e, x: xOf(e), y: dotY(e) }))
              const xs = pts.map((p) => p.x)
              const minX = Math.min(...xs) - 14
              const maxX = Math.max(...xs) + 14
              const minY = Math.min(...pts.map((p) => p.y)) - 9
              const hi = tooltip?.series_id === sid
              return (
                <g key={sid} className={hi ? 'rt-hull highlighted' : 'rt-hull'}>
                  <rect
                    x={minX} y={minY} width={maxX - minX} height={baselineY - minY}
                    rx={9} className={hi ? 'rt-hull-bg highlighted' : 'rt-hull-bg'}
                  />
                  {pts.map((p) => (
                    <line key={p.e.id} x1={p.x} y1={p.y} x2={p.x} y2={baselineY} className={hi ? 'rt-stem highlighted' : 'rt-stem'} />
                  ))}
                </g>
              )
            })}

            {/* event dots: 1px stems from the baseline up to dots above it.
                Every event (one per distinct date, spec A2) always gets its
                own dot + stem + hull stem; nothing is deduplicated by
                encounter_id. Only truly-overlapping dots (bubbleGroups)
                collapse, and even then each remains a focusable dot. */}
            {events.map((ev) => {
              const d = parseISO(ev.date)!
              const x = xOf(ev)
              if (x < -20 || x > chartW + 20) return null
              const y = dotY(ev)
              const preBirth = !!birth && d < birth
              const future = !!today && d > today
              const selected = tooltipEv === ev.id
              const seriesIdx = ev.series_id
                ? events.filter((e) => e.series_id === ev.series_id).findIndex((e) => e.id === ev.id) + 1
                : 0
              const label = `${ev.date} — ${ev.title}${ev.facility ? ` at ${ev.facility}` : ''} — ${KIND_LABEL[ev.kind] ?? ev.kind}${seriesIdx ? ` — ${seriesIdx} of ${ev.series_size} in series` : ''}`
              const bubble = bubbles.get(ev.id)
              return (
                <g key={ev.id}>
                  <line
                    x1={x} y1={y} x2={x}
                    y2={baselineY}
                    className={selected ? 'rt-stem selected' : 'rt-stem'}
                  />
                  {selected && (
                    <line
                      x1={x} y1={y} x2={x}
                      y2={y - TOOLTIP_GAP_PX}
                      className="rt-leader"
                    />
                  )}
                  {selected && (
                    <circle cx={x} cy={y} r={9} className="rt-dot-ring" />
                  )}
                  <circle
                    cx={x} cy={y} r={selected ? 5 : 4.5}
                    className={`rt-dot${selected ? ' selected' : ''}${preBirth ? ' pre-birth' : ''}${future ? ' future' : ''}`}
                    style={{ fill: KIND_COLORS[ev.kind] ?? KIND_COLORS.other }}
                  />
                  <circle
                    cx={x} cy={y} r={12}
                    className="rt-dot-hit"
                    role="button"
                    tabIndex={0}
                    aria-label={label}
                    onMouseEnter={() => setHovered(ev.id)}
                    onMouseLeave={() => setHovered(null)}
                    onFocus={() => setFocused(ev.id)}
                    onBlur={() => setFocused(null)}
                    onClick={() => {
                      // touch: first tap tooltip, second opens (the card is
                      // pointer-events:none, so the second tap reaches here)
                      if (hovered === ev.id && focusedIdx >= 0) { onOpen(ev) }
                      else { setHovered(ev.id); setFocused(ev.id); setFocusedIdx(1) }
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(ev) }
                      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                        e.preventDefault()
                        const idx = events.findIndex((el) => el.id === ev.id)
                        const next = events[idx + (e.key === 'ArrowRight' ? 1 : -1)]
                        if (next) {
                          const el = document.querySelector(`[aria-label="${CSS.escape(next.id)}"]`) as any
                          // focus by order: re-render targets have aria-label = label; use id data attr instead
                          document.querySelector<HTMLElement>(`[data-event-id="${next.id}"]`)?.focus()
                          void el
                        }
                      }
                    }}
                    data-event-id={ev.id}
                  />
                  {bubble && bubble.evs[0] === ev.id && (
                    <text x={x} y={y - 10} className="rt-bubble-label">
                      +{bubble.evs.length - 1}
                    </text>
                  )}
                </g>
              )
            })}
          </svg>

          {/* Selected/hovered event card: ALWAYS above its dot, centered
              on it via tooltipCardPos (v5), with a straight vertical leader
              line in the SVG from the dot up to the card's bottom edge
              (x = dotX). pointer-events:none keeps the dot clickable. */}
          {tooltip && (() => {
            const pos = tooltipCardPos(xOf(tooltip), dotY(tooltip), chartW)
            // v5 bottom-anchored: the card's bottom edge sits exactly
            // TOOLTIP_GAP_PX above the dot, so the straight leader line
            // (SVG, x=dotX, y in [dotY-GAP, dotY]) meets the card's bottom
            // edge regardless of the card's actual rendered height.
            const dotYpx = dotY(tooltip)
            const bottom = chartH - (dotYpx - TOOLTIP_GAP_PX)
            // Keep the popup readable inside the visible viewport: clamp
            // its left to the scroller's visible window (centered on the
            // dot whenever it fits, shifted only near an edge).
            const viewW = scrollerRef.current?.clientWidth ?? vw
            const viewLeft = scrollLeft + 4
            const viewRight = scrollLeft + Math.max(232, viewW) - 232
            const left = Math.max(viewLeft, Math.min(pos.left, Math.max(viewLeft, viewRight)))
            return (
            <div
              className="rt-card"
              role="status"
              style={{ left, bottom, top: 'auto' }}
            >
              <div className="rt-card-date">{tooltip.date}</div>
              <div className="rt-card-title">{tooltip.title}</div>
              <div className="rt-card-sub">
                {[tooltip.facility, KIND_LABEL[tooltip.kind] ?? tooltip.kind].filter(Boolean).join(' · ')}
                {tooltip.series_id && (
                  <> · {events.filter((e) => e.series_id === tooltip.series_id).findIndex((e) => e.id === tooltip.id) + 1} of {tooltip.series_size} in series</>
                )}
              </div>
            </div>
            )
          })()}
        </div>
      </div>
    </div>
  )
}
