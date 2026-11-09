// @ts-nocheck
/** Intake chart legend items + x-axis bottom layout (§4.7 legend fix).
 *  Pure data/constants (no JSX) so they are unit-testable with node:test.
 *  Labels use the same terms as the chart tooltip: Supplements, Food,
 *  UL, No data, daily reference. */

export type LegendSwatch =
  | 'supp' // solid green segment (top of stack)
  | 'food' // light green 55% opacity (bottom of stack)
  | 'hatch' // dashed/hatched food segment (incomplete data)
  | 'ref' // thick line at 100%
  | 'ul' // golden dashed line (UL level)
  | 'above-ul' // golden border outline on a bar (above UL)
  | 'no-data' // grey tick on baseline
  | 'dot' // one dot per day (month)
  | 'whisker' // min-max whisker line (month)

export interface LegendItem {
  key: string
  label: string
  swatch: LegendSwatch
  views: 'all' | ('week' | 'month')[]
}

export const LEGEND_ITEMS: LegendItem[] = [
  { key: 'supplements', label: 'Supplements', swatch: 'supp', views: 'all' },
  { key: 'food', label: 'Food', swatch: 'food', views: 'all' },
  { key: 'food-incomplete', label: 'Food, incomplete data (<80% kcal)', swatch: 'hatch', views: 'all' },
  { key: 'daily-reference', label: '100% daily reference', swatch: 'ref', views: 'all' },
  { key: 'ul', label: 'Upper limit (UL)', swatch: 'ul', views: 'all' },
  { key: 'above-ul', label: 'Above UL (upper limit)', swatch: 'above-ul', views: 'all' },
  { key: 'no-data', label: 'No data that day', swatch: 'no-data', views: 'all' },
  { key: 'one-day', label: 'One day (month view)', swatch: 'dot', views: ['month'] },
  { key: 'min-max', label: 'Min-max range (month)', swatch: 'whisker', views: ['month'] },
]

export function legendItemsFor(view: 'week' | 'month'): LegendItem[] {
  return LEGEND_ITEMS.filter((i) => i.views === 'all' || i.views.includes(view))
}

/** X-axis layout below the baseline (px). Nutrient labels sit *under* the
 *  baseline: the chart area ends at the baseline, labels start below it,
 *  so they can never penetrate the plot. Month labels are end-anchored and
 *  rotated -45°, so the whole rotated text sits below the anchor point. */
export const AXIS_OFFSETS = {
  week: { nutrient: 14, weekday: 26, groupLine: 36, groupText: 50, bottom: 60 },
  month: { nutrientAnchor: 12, groupLine: 84, groupText: 98, bottom: 106 },
} as const

export function bottomLabelPx(view: 'week' | 'month'): number {
  return AXIS_OFFSETS[view].bottom
}

/** Bound nutrient-label length so rotated month labels stay within the
 *  bottom margin and horizontal labels don't collide with neighbours. */
export const MAX_LABEL_CHARS = 14
export function truncateLabel(label: string, max: number = MAX_LABEL_CHARS): string {
  return label.length <= max ? label : `${label.slice(0, max - 1)}…`
}
