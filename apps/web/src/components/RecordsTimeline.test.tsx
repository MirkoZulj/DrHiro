// @ts-nocheck
/**
 * RecordsTimeline series-layout tests (no web test runner in this repo yet —
 * same pattern as views/Records.test.tsx; @ts-nocheck keeps tsc out).
 *
 * Regression: multi-date records (series_size=3) rendered as a single dot.
 * Root causes: (1) bubble grouping keyed on the exact rounded x — series
 * dots sharing a pixel were grouped into a "+n" bubble and lost their
 * stems; (2) no minimum display gap, so distinct dates days apart at
 * lifetime zooms (pxPerDay floored at 0.5) drew concentric dots.
 * Spec A2 (one event per distinct date, same-date records share
 * record_ids[]) is unchanged — the fix is purely in display layout.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  MIN_SERIES_GAP_PX,
  MIN_DOT_SEPARATION_PX,
  seriesDisplayX,
  assignLanes,
  bubbleGroups,
  hashJitter,
  dotYFor,
  CENTER_BAND_TOP,
  CENTER_BAND_BOTTOM,
  UPPER_BAND_TOP,
  UPPER_BAND_BOTTOM,
  CHART_H_DESKTOP,
  CHART_H_MOBILE,
  DEFAULT_ZOOM,
  ZOOMS,
  QUARTER_MONTHS,
  MIN_QUARTER_PX,
  MAX_CHART_W,
  ZOOM_SPAN_DAYS,
  MIN_ALL_PX_PER_DAY,
  quarterMonthTicks,
  zoomPxPerDay,
  TOOLTIP_GAP_PX,
  TOOLTIP_CARD_EST_H,
  TOOLTIP_CARD_HALF_W,
  tooltipCardPos,
} from './RecordsTimeline'

const ev = (id, date, seriesId = null) => ({
  id,
  encounter_id: 'enc-1',
  date,
  kind: 'lab',
  title: 'T',
  facility: null,
  record_ids: [],
  series_id: seriesId ?? (seriesId === null ? null : seriesId),
  series_size: seriesId ? 3 : 1,
})

describe('seriesDisplayX', () => {
  it('spreads close series dates to >= MIN_SERIES_GAP_PX, first dot unmoved', () => {
    // 0.5 px/day: 2-day gaps collapse to the same rounded x.
    const events = [
      ev('a', '2026-09-01', 's1'),
      ev('b', '2026-09-03', 's1'),
      ev('c', '2026-09-05', 's1'),
    ]
    const xFor = (d) => Math.round((d - new Date('2026-09-01T00:00:00')) / 86400000 / 2)
    const m = seriesDisplayX(events, xFor)
    assert.equal(m.get('a'), 0)
    assert.ok(m.get('b') - m.get('a') >= MIN_SERIES_GAP_PX)
    assert.ok(m.get('c') - m.get('b') >= MIN_SERIES_GAP_PX)
    assert.equal(new Set(m.values()).size, 3) // 3 distinct dots, not 1
  })

  it('leaves well-separated series dates and non-series events untouched', () => {
    const events = [
      ev('a', '2026-01-10', 's1'),
      ev('b', '2026-05-11', 's1'),
      ev('c', '2026-09-03', 's1'),
      ev('d', '2026-09-03', null),
    ]
    const xFor = (d) => Math.round((d - new Date('2026-01-01T00:00:00')) / 86400000 * 2)
    const m = seriesDisplayX(events, xFor)
    assert.equal(m.get('a'), xFor(new Date('2026-01-10T00:00:00')))
    assert.equal(m.get('b'), xFor(new Date('2026-05-11T00:00:00')))
    assert.equal(m.get('c'), xFor(new Date('2026-09-03T00:00:00')))
    assert.equal(m.get('d'), m.get('c')) // non-series keeps true x
  })

  it('does not deduplicate by encounter_id — distinct ids stay distinct', () => {
    const events = [ev('enc-1:2026-09-01', '2026-09-01', 'enc-1'), ev('enc-1:2026-09-02', '2026-09-02', 'enc-1')]
    const m = seriesDisplayX(events, () => 100)
    assert.equal(m.size, 2)
    assert.notEqual(m.get('enc-1:2026-09-01'), m.get('enc-1:2026-09-02'))
  })
})

describe('assignLanes + bubbleGroups', () => {
  it('same-x series dots stepped to different lanes are never bubbled', () => {
    const items = [
      { id: 'a', x: 100, y: 110, lane: 0 },
      { id: 'b', x: 100, y: 98, lane: 1 },
      { id: 'c', x: 100, y: 86, lane: 2 },
    ]
    assert.equal(bubbleGroups(items).size, 0)
  })

  it('assignLanes gives close dots distinct lanes', () => {
    const lanes = assignLanes([
      { id: 'a', x: 100 },
      { id: 'b', x: 101 },
      { id: 'c', x: 102 },
    ])
    assert.deepEqual([lanes.get('a'), lanes.get('b'), lanes.get('c')], [0, 1, 2])
  })

  it('only same-lane dots < MIN_DOT_SEPARATION_PX (lanes exhausted) bubble', () => {
    // 6 dots at the same x exhaust lanes 0..4; the 6th reuses lane 4 <8px from the 5th.
    const items = [0, 1, 2, 3, 4, 4].map((lane, i) => ({ id: `e${i}`, x: 100, y: 110 - lane * 12, lane }))
    const b = bubbleGroups(items)
    assert.ok(b.has('e4') && b.has('e5'))
    assert.equal(b.get('e4').evs.length, 2)
    // lane-separated dots never appear in any bubble
    for (const id of ['e0', 'e1', 'e2', 'e3']) assert.equal(b.has(id), false)
  })

  it('MIN_DOT_SEPARATION_PX is 8', () => {
    assert.equal(MIN_DOT_SEPARATION_PX, 8)
  })
})

describe('timeline v3 geometry', () => {
  it('frame is twice as big: 320 desktop / 280 mobile', () => {
    assert.equal(CHART_H_DESKTOP, 320)
    assert.equal(CHART_H_MOBILE, 280)
  })

  it('default span is 3 years; zooms are [1y|3y|5y|10y|All]', () => {
    assert.equal(DEFAULT_ZOOM, '3y')
    assert.deepEqual([...ZOOMS], ['1y', '3y', '5y', '10y', 'All'])
  })

  it('v5 center band is 40%-60% of chart height', () => {
    assert.equal(CENTER_BAND_TOP, 0.40)
    assert.equal(CENTER_BAND_BOTTOM, 0.60)
    // legacy aliases track the center band
    assert.equal(UPPER_BAND_TOP, 0.40)
    assert.equal(UPPER_BAND_BOTTOM, 0.60)
  })

  it('hashJitter is deterministic and in [0, 1)', () => {
    assert.equal(hashJitter('evt-1'), hashJitter('evt-1'))
    for (const id of ['a', 'b', 'evt-42', '2026-09-01', 'x'.repeat(40)]) {
      const j = hashJitter(id)
      assert.ok(j >= 0 && j < 1, `${id} -> ${j}`)
    }
  })

  it('hashJitter spreads ids (not all near the same value)', () => {
    const vals = Array.from({ length: 20 }, (_, i) => hashJitter(`ev-${i}`))
    assert.ok(Math.max(...vals) - Math.min(...vals) > 0.5)
  })

  it('v5 dotYFor lands in the center band for desktop + mobile heights', () => {
    for (const h of [CHART_H_DESKTOP, CHART_H_MOBILE]) {
      for (let i = 0; i < 50; i++) {
        const y = dotYFor(`ev-${i}`, h)
        assert.ok(y >= h * CENTER_BAND_TOP - 0.01 && y <= h * CENTER_BAND_BOTTOM + 0.01,
          `h=${h} i=${i} y=${y}`)
        // never in the old upper third
        assert.ok(y > h * 0.35, `h=${h} i=${i} y=${y} still in upper third`)
      }
    }
  })

  it('dotYFor is deterministic (stable across renders/zooms)', () => {
    assert.equal(dotYFor('evt-7', 320), dotYFor('evt-7', 320))
  })

  it('dots are spread, not stacked at one level', () => {
    const ys = new Set(Array.from({ length: 20 }, (_, i) => dotYFor(`ev-${i}`, 320)))
    assert.ok(ys.size >= 15, `only ${ys.size} distinct heights`)
  })
})

describe('timeline v4 — full range, 2-3y window', () => {
  const birth = new Date(1979, 0, 20)
  const today = new Date(2026, 10, 9)
  const spanDays = Math.round((new Date(today.getFullYear() + 1, today.getMonth(), today.getDate()) - birth) / 86400000)

  it('zoomPxPerDay at 3y shows ~3 years across the viewport', () => {
    const vw = 1200
    const px = zoomPxPerDay(vw, '3y', spanDays)
    assert.ok(Math.abs(vw / px - 3 * 365) < 1, `visible days ${vw / px}`)
    // 1y on mobile (375px) shows ~1y
    const pxMobile1y = zoomPxPerDay(375, '1y', spanDays)
    assert.ok(Math.abs(375 / pxMobile1y - 365) < 1, `1y visible days ${375 / pxMobile1y}`)
  })

  it('full range stays scrollable at 3y: chart is much wider than the viewport', () => {
    const vw = 1200
    const px = zoomPxPerDay(vw, '3y', spanDays)
    const chartW = Math.round(spanDays * px)
    assert.ok(chartW > 5 * vw, `chartW ${chartW} vs vw ${vw}`) // ~47y lifespan at 3y window
  })

  it('All zoom sizes to the full span (chartW ~= viewport)', () => {
    const vw = 1200
    const px = zoomPxPerDay(vw, 'All', spanDays)
    assert.ok(Math.abs(spanDays * px - vw) < 2, `All chartW ${spanDays * px}`)
  })
})

describe('timeline zoom fix 2026-11-09 — exact viewport spans', () => {
  // Owner: "I press 10y and see 4, I press All I see 20y, no change 1y/3y."
  // Root causes: (1) MAX_CHART_W cap bound 1y AND 3y to the same pxPerDay
  // on wide screens; (2) vw was window.innerWidth (2560) while the
  // scroller is ~1114px wide in the max-width:1180px .app column, so the
  // visible span was ~0.44x of intended (10y→4.4y, All→21y). The span
  // below mirrors the component: birth 1979 → today+1y.
  const birth = new Date(1979, 0, 20)
  const today = new Date(2026, 10, 9)
  const spanDays = Math.round((new Date(today.getFullYear() + 1, today.getMonth(), today.getDate()) - birth) / 86400000)
  const VIEWPORTS = [375, 800, 1114, 1280, 1920, 2560]

  it('ZOOM_SPAN_DAYS maps each N-year zoom to exactly N*365 days', () => {
    assert.deepEqual({ ...ZOOM_SPAN_DAYS }, { '1y': 365, '3y': 1095, '5y': 1825, '10y': 3650 })
  })

  it('every N-year zoom shows EXACTLY its span at every viewport width', () => {
    for (const vw of VIEWPORTS) {
      for (const [zoom, days] of Object.entries(ZOOM_SPAN_DAYS)) {
        const px = zoomPxPerDay(vw, zoom, spanDays)
        assert.ok(Math.abs(vw / px - days) < 0.01,
          `${zoom} @vw ${vw}: visible ${vw / px} days, want ${days}`)
      }
    }
  })

  it('pxPerDay strictly decreases 1y > 3y > 5y > 10y > All at every width', () => {
    for (const vw of VIEWPORTS) {
      const pxs = ['1y', '3y', '5y', '10y', 'All'].map((z) => zoomPxPerDay(vw, z, spanDays))
      for (let i = 1; i < pxs.length; i++) {
        assert.ok(pxs[i - 1] > pxs[i], `vw ${vw}: ${pxs[i - 1]} !> ${pxs[i]}`)
      }
    }
  })

  it('regression: 1y and 3y differ on a wide screen (cap used to bind both)', () => {
    for (const vw of [1920, 2560]) {
      assert.notEqual(zoomPxPerDay(vw, '1y', spanDays), zoomPxPerDay(vw, '3y', spanDays))
      assert.ok(Math.abs(vw / zoomPxPerDay(vw, '1y', spanDays) - 365) < 0.01)
      assert.ok(Math.abs(vw / zoomPxPerDay(vw, '3y', spanDays) - 1095) < 0.01)
    }
  })

  it('regression: 10y at the real scroller width shows 10y, not 4y', () => {
    const vw = 1114 // ~1180px .app column minus padding/borders
    const px = zoomPxPerDay(vw, '10y', spanDays)
    assert.ok(Math.abs(vw / px - 3650) < 0.01, `visible ${(vw / px / 365).toFixed(2)}y`)
    assert.ok(spanDays * px > 3 * vw, '10y chart stays scrollable')
  })

  it('1y chart may exceed MAX_CHART_W (exact span beats the old cap)', () => {
    const px = zoomPxPerDay(1114, '1y', spanDays)
    assert.ok(spanDays * px > MAX_CHART_W, `chartW ${spanDays * px}`)
  })

  it("All fits the full span, floored at MIN_ALL_PX_PER_DAY for tiny widths", () => {
    assert.equal(MIN_ALL_PX_PER_DAY, 0.05)
    // 375px viewport over a ~48y span: 375/17.5k ≈ 0.021 → floored, scrollable
    assert.equal(zoomPxPerDay(375, 'All', spanDays), MIN_ALL_PX_PER_DAY)
    // desktop: above the floor, exact fit
    const px = zoomPxPerDay(1114, 'All', spanDays)
    assert.ok(px > MIN_ALL_PX_PER_DAY)
    assert.ok(Math.abs(spanDays * px - 1114) < 0.01)
  })

  it('component measures the scroller clientWidth, not window.innerWidth', () => {
    const tsx = readFileSync(fileURLToPath(new URL('./RecordsTimeline.tsx', import.meta.url)), 'utf8')
    assert.match(tsx, /el\.clientWidth/)
    assert.match(tsx, /ResizeObserver/)
    assert.doesNotMatch(tsx, /setVw\(window\.innerWidth\)/)
    assert.match(tsx, /zoomPxPerDay\(vw, zoom, spanDays\)/)
  })

  it("'All' pins scroll to the start on zoom change", () => {
    const tsx = readFileSync(fileURLToPath(new URL('./RecordsTimeline.tsx', import.meta.url)), 'utf8')
    assert.match(tsx, /if \(zoom === 'All'\) scrollerRef\.current\?\.scrollTo\(\{ left: 0/)
  })
})

describe('timeline v4 — Mar/Jun/Sep axis labels', () => {
  const start = new Date(2023, 10, 9) // ~3y before Nov 2026
  const end = new Date(2027, 10, 9)
  const pxPerDay = 1200 / (3 * 365) // desktop 3y
  const chartW = 20000
  const xFor = (d) => Math.round(((d - start) / 86400000) * pxPerDay)

  it('QUARTER_MONTHS are Mar / Jun / Sep at month indices 2 / 5 / 8', () => {
    assert.deepEqual(QUARTER_MONTHS.map((q) => q.label), ['Mar', 'Jun', 'Sep'])
    assert.deepEqual(QUARTER_MONTHS.map((q) => q.month), [2, 5, 8])
  })

  it('at 3y zoom each year gets Mar, Jun, Sep labels at the 1st of those months', () => {
    const ticks = quarterMonthTicks(start, end, xFor, pxPerDay, chartW)
    assert.ok(ticks.length >= 9, `expected >=9 quarter ticks, got ${ticks.length}`)
    for (const y of [2024, 2025, 2026]) {
      const labels = ticks.filter((t) => t.year === y).map((t) => t.label)
      assert.deepEqual(labels, ['Mar', 'Jun', 'Sep'])
    }
    const mar2026 = ticks.find((t) => t.year === 2026 && t.label === 'Mar')
    assert.equal(mar2026.x, xFor(new Date(2026, 2, 1)))
    const sep2026 = ticks.find((t) => t.year === 2026 && t.label === 'Sep')
    assert.equal(sep2026.x, xFor(new Date(2026, 8, 1)))
  })

  it('ticks are spaced ~91 days apart at 3y', () => {
    const ticks = quarterMonthTicks(start, end, xFor, pxPerDay, chartW)
    const gap = ticks[1].x - ticks[0].x
    assert.ok(gap >= MIN_QUARTER_PX, `gap ${gap}`)
    assert.ok(Math.abs(gap - 91 * pxPerDay) < 3, `gap ${gap} vs ${91 * pxPerDay}`)
  })

  it('labels thin out when quarter spacing is too small (All zoom)', () => {
    assert.deepEqual(quarterMonthTicks(start, end, xFor, 0.1, chartW), [])
    assert.deepEqual(quarterMonthTicks(start, end, xFor, 0.2, chartW), []) // 18px < 28
    assert.ok(quarterMonthTicks(start, end, xFor, 0.4, chartW).length > 0) // 36px >= 28
  })
})

describe('timeline v4 — green-text buttons', () => {
  const css = readFileSync(fileURLToPath(new URL('../index.css', import.meta.url)), 'utf8')
  const rule = (sel) => {
    const m = css.match(new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'))
    assert.ok(m, `rule ${sel} not found`)
    return m[1]
  }

  it('.rt-zoom is transparent with green text (not white-on-white)', () => {
    const body = rule('.rt-zoom')
    assert.match(body, /background:\s*transparent/)
    assert.match(body, /color:\s*var\(--color-accent-text\)/)
  })

  it('.rt-zoom hover/active fill green with white text', () => {
    const hover = css.match(/\.rt-zoom:hover[^{]*\{([^}]*)\}/)
    assert.ok(hover && /background:\s*var\(--color-accent-strong\)/.test(hover[1]) && /color:\s*#fff/.test(hover[1]))
    const active = rule('.rt-zoom.active')
    assert.match(active, /background:\s*var\(--color-accent-strong\)/)
    assert.match(active, /color:\s*#fff/)
  })

  it('.rt-today uses the same transparent/green-text/hover pattern', () => {
    const body = rule('.rt-today')
    assert.match(body, /background:\s*transparent/)
    assert.match(body, /color:\s*var\(--color-accent-text\)/)
    const hover = css.match(/\.rt-today:hover[^{]*\{([^}]*)\}/)
    assert.ok(hover && /background:\s*var\(--color-accent-strong\)/.test(hover[1]) && /color:\s*#fff/.test(hover[1]))
  })

  it('.records-timeline-scroller is a horizontal scroll container (regression 2026-11-09)', () => {
    // Prod web 0.12.0-c shipped timeline JS with a micros-branch index.css
    // that had NO .rt-* rules: computed overflow was `visible`, so the
    // scroller could never scroll (scrollLeft stuck at 0), the 18k-54k px
    // chart overflowed the page, and Records looked blank at 1980.
    const body = rule('.records-timeline-scroller')
    assert.match(body, /overflow-x:\s*auto/)
    assert.match(body, /overflow-y:\s*hidden/)
    // Every class the component renders must have a rule: a missing rule
    // silently degrades layout instead of failing loudly.
    for (const sel of ['.records-timeline-wrap', '.records-timeline-header',
      '.rt-zoom-group', '.rt-zoom', '.rt-today', '.rt-legend', '.rt-legend-dot',
      '.rt-chart', '.rt-svg', '.rt-baseline', '.rt-year-grid', '.rt-tick',
      '.rt-tick-label', '.rt-marker', '.rt-marker-label', '.rt-stem',
      '.rt-dot', '.rt-dot-hit', '.rt-hull-bg', '.rt-card']) {
      assert.ok(css.includes(sel), `index.css missing ${sel}`)
    }
  })

  it('index.css keeps the micros intake-legend rules (branch-union guard)', () => {
    // The timeline branch and the 0.12.0-micros branch both edit index.css;
    // prod is built from a tree containing both feature sets, so neither
    // side's rules may be dropped when syncing.
    assert.ok(css.includes('.intake-legend'), 'index.css missing .intake-legend')
    assert.match(css, /\.intake-legend\s*\{/)
  })
})

describe('timeline v5 tooltip — always above, straight leader', () => {
  const css = readFileSync(fileURLToPath(new URL('../index.css', import.meta.url)), 'utf8')
  const tsx = readFileSync(fileURLToPath(new URL('./RecordsTimeline.tsx', import.meta.url)), 'utf8')
  const rule = (sel) => {
    const m = css.match(new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'))
    assert.ok(m, `rule ${sel} not found`)
    return m[1]
  }

  it('gap is 16-24px and card estimate covers the rendered card', () => {
    assert.ok(TOOLTIP_GAP_PX >= 16 && TOOLTIP_GAP_PX <= 24, `gap ${TOOLTIP_GAP_PX}`)
    assert.equal(TOOLTIP_GAP_PX, 20)
    assert.ok(TOOLTIP_CARD_EST_H >= 78, `est ${TOOLTIP_CARD_EST_H}`) // ~78.5px: 13 pad + 3-4 lines + border
  })

  it('v5 for every dot in the center band the card is above and never overlaps its dot', () => {
    for (const h of [CHART_H_DESKTOP, CHART_H_MOBILE]) {
      for (let i = 0; i < 200; i++) {
        const y = dotYFor(`ev-${i}`, h)
        const pos = tooltipCardPos(1000, y, 20000)
        assert.equal(pos.placement, 'above')
        assert.ok(pos.top + TOOLTIP_CARD_EST_H <= y - TOOLTIP_GAP_PX + 0.001,
          `h=${h} y=${y} card bottom ${pos.top + TOOLTIP_CARD_EST_H}`)
        assert.ok(pos.top >= 2, `top ${pos.top} off chart`)
        assert.ok(pos.top < y, `popup not above dot: top ${pos.top} dotY ${y}`)
      }
    }
  })

  it('v5 placement is always above (below/flip logic removed)', () => {
    for (const y of [48, 112, 128, 160, 192, 250]) {
      assert.equal(tooltipCardPos(1000, y, 20000).placement, 'above', `y=${y}`)
    }
    // center-band dots: top = dotY - GAP - EST, no clamping needed
    assert.equal(tooltipCardPos(1000, 128, 20000).top, 128 - TOOLTIP_GAP_PX - TOOLTIP_CARD_EST_H)
    assert.equal(tooltipCardPos(1000, 192, 20000).top, 192 - TOOLTIP_GAP_PX - TOOLTIP_CARD_EST_H)
  })

  it('v5 card is centered on the dot x (left = dotX - 114) and clamped inside', () => {
    assert.equal(TOOLTIP_CARD_HALF_W, 114)
    assert.equal(tooltipCardPos(1000, 160, 20000).left, 1000 - 114)
    assert.equal(tooltipCardPos(10, 160, 20000).left, 4)
    assert.equal(tooltipCardPos(19990, 160, 20000).left, 20000 - 232)
    // centered card: dot x is the card center, so a straight vertical
    // leader at x=dotX meets the card's bottom-center arrow
    const pos = tooltipCardPos(1000, 160, 20000)
    assert.equal(pos.left + TOOLTIP_CARD_HALF_W, 1000)
  })

  it('.rt-card is pointer-events:none so the dot stays clickable under it', () => {
    assert.match(rule('.rt-card'), /pointer-events:\s*none/)
  })

  it('v5 no below placement remains (no .rt-card.below rule, no below class in tsx)', () => {
    assert.doesNotMatch(css, /\.rt-card\.below/)
    assert.doesNotMatch(tsx, /rt-card below/)
    assert.doesNotMatch(tsx, /'below'/)
  })

  it('v5 straight vertical leader: SVG line x1==x2==dotX, y1=dotY, y2=dotY-GAP', () => {
    assert.match(tsx, /className="rt-leader"/)
    assert.match(tsx, /x1=\{x\} y1=\{y\} x2=\{x\}/)
    assert.match(tsx, /y2=\{y - TOOLTIP_GAP_PX\}/)
    assert.match(rule('.rt-leader'), /pointer-events:\s*none/)
    // leader is vertical, never diagonal: no x2 expression other than x
    assert.doesNotMatch(tsx, /rt-leader[\s\S]{0,120}x2=\{[^}]*[^x]\}/)
  })

  it('component positions the card via tooltipCardPos and the display x', () => {
    assert.match(tsx, /tooltipCardPos\(xOf\(tooltip\), dotY\(tooltip\), chartW\)/)
    assert.match(tsx, /className="rt-card"/)
    assert.match(tsx, /style=\{\{ left, bottom, top: 'auto' \}\}/)
    assert.match(tsx, /const bottom = chartH - \(dotYpx - TOOLTIP_GAP_PX\)/)
    // old bug: card was left-anchored II on the raw date x, not the display x
    assert.doesNotMatch(tsx, /xFor\(parseISO\(tooltip\.date\)!\)/)
  })

  it('.rt-dot-hit is an invisible r=12 hit area carrying the handlers', () => {
    const body = rule('.rt-dot-hit')
    assert.match(body, /fill:\s*transparent/)
    assert.match(body, /pointer-events:\s*all/)
    assert.match(body, /cursor:\s*pointer/)
    assert.match(tsx, /r=\{12\}/)
    assert.match(tsx, /className="rt-dot-hit"/)
    // visible dot is pointer-events:none so it can't steal the hit area
    assert.match(rule('.rt-dot'), /pointer-events:\s*none/)
  })
})
