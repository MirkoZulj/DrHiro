// @ts-nocheck
// Intake legend + x-axis layout tests (node:test, no web test runner yet —
// same pattern as Records.test.tsx; run with:
//   node --experimental-strip-types --test src/components/IntakeLegend.test.ts
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  AXIS_OFFSETS,
  LEGEND_ITEMS,
  MAX_LABEL_CHARS,
  bottomLabelPx,
  legendItemsFor,
  truncateLabel,
} from './IntakeLegend.ts'

const keysOf = (items) => items.map((i) => i.key)

describe('intake legend presence', () => {
  it('covers every chart element: solid, light, hatched, ref, UL, above-UL, tick, dot, whisker', () => {
    const keys = keysOf(LEGEND_ITEMS)
    for (const k of ['supplements', 'food', 'food-incomplete', 'daily-reference', 'ul', 'above-ul', 'no-data', 'one-day', 'min-max']) {
      assert.ok(keys.includes(k), `legend missing ${k}`)
    }
  })

  it('every legend item has a non-empty label and a swatch', () => {
    for (const i of LEGEND_ITEMS) {
      assert.ok(i.label && i.label.length > 0, `empty label for ${i.key}`)
      assert.ok(i.swatch, `missing swatch for ${i.key}`)
    }
  })

  it('week view shows the 7 base items, no month-only items', () => {
    const keys = keysOf(legendItemsFor('week'))
    assert.equal(keys.length, 7)
    assert.ok(!keys.includes('one-day'))
    assert.ok(!keys.includes('min-max'))
  })

  it('month view adds dot and whisker items', () => {
    const keys = keysOf(legendItemsFor('month'))
    assert.ok(keys.includes('one-day'))
    assert.ok(keys.includes('min-max'))
    assert.ok(keys.includes('food-incomplete'))
    assert.ok(keys.includes('above-ul'))
  })

  it('legend terms match tooltip wording (Supplements/Food/UL/No data/daily reference)', () => {
    const labels = LEGEND_ITEMS.map((i) => i.label).join(' | ')
    for (const term of ['Supplements', 'Food', 'UL', 'No data', 'daily reference']) {
      assert.ok(labels.includes(term), `legend missing tooltip term: ${term}`)
    }
  })
})

describe('x-axis bottom padding (labels never penetrate the chart)', () => {
  it('month bottom margin exceeds week bottom margin', () => {
    assert.ok(bottomLabelPx('month') > bottomLabelPx('week'))
  })

  it('group caption text + descender fits inside the bottom margin (both views)', () => {
    for (const v of ['week', 'month']) {
      assert.ok(AXIS_OFFSETS[v].bottom >= AXIS_OFFSETS[v].groupText + 6, `${v}: group text overflows bottom margin`)
    }
  })

  it('week nutrient labels start below the baseline with clearance', () => {
    assert.ok(AXIS_OFFSETS.week.nutrient >= 10, 'week labels too close to baseline')
    assert.ok(AXIS_OFFSETS.week.groupLine > AXIS_OFFSETS.week.weekday, 'weekday row overlaps group line')
    assert.ok(AXIS_OFFSETS.week.groupText > AXIS_OFFSETS.week.groupLine, 'group text overlaps group line')
  })

  it('rotated month labels: end-anchored and fully below the baseline', () => {
    const m = AXIS_OFFSETS.month
    // Worst-case rotated drop for MAX_LABEL_CHARS chars at ~5px/char (9px font):
    // vertical drop = chars * charW * sin(45°)
    const worstDrop = MAX_LABEL_CHARS * 5 * Math.SQRT1_2
    assert.ok(m.nutrientAnchor >= 8, 'month anchor too close to baseline')
    assert.ok(m.nutrientAnchor + worstDrop + 4 <= m.groupLine, 'rotated label reaches group line')
    assert.ok(m.groupLine < m.groupText, 'group line/text order')
  })

  it('truncateLabel bounds label length', () => {
    assert.equal(truncateLabel('Vitamin C'), 'Vitamin C')
    const long = truncateLabel('Docosahexaenoic acid (DHA)')
    assert.ok(long.length <= MAX_LABEL_CHARS, `long label not truncated: ${long}`)
    assert.ok(long.endsWith('…'), 'truncated label missing ellipsis')
  })
})
