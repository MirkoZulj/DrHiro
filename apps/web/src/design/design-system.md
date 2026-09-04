# drHiro Design System

A calm, clinical, trustworthy visual language for a personal health platform.
drHiro tracks steps, sleep, weight, blood pressure, meals, and reminders. It is
a daily companion users trust with sensitive health data — so the UI must feel
**quiet, legible, and dependable**: soft neutral grays anchored by a single
trustworthy teal/green accent, generous whitespace, restrained elevation, and
data that is honest about what is known vs. missing.

> **Consume via tokens.** Every value below is a CSS custom property defined in
> [`tokens.css`](./tokens.css) (light = `:root`, dark = `[data-theme="dark"]`).
> Component code should reference **tokens only** — never hardcoded hex values.

---

## 1. Design principles

1. **Calm by default.** No loud colors competing for attention. The accent is
   used sparingly — for the primary action and the single most important figure
   on a screen. Neutrals carry the layout.
2. **Trust through honesty.** Health data must never imply precision it doesn't
   have. **Missing data is missing — never zero.** Missing values render as
   dashed/outlined placeholders (`--`), never as `0` bars or flat lines.
3. **Legible first.** Body text hits WCAG AA (≥ 4.5:1); large text and icons
   hit AA large (≥ 3:1). Default font size is 16px — never smaller than 13px
   for interactive/body content.
4. **One accent, many meanings.** One teal/green brand accent. Semantic colors
   (success/warning/danger/info) communicate *state*, not brand — used on
   backgrounds with adequate contrast, never as brand decoration.
5. **Clinical but warm.** Sharp enough to read like a chart, soft enough to
   open daily. Rounded corners are present but modest; shadows are low and
   diffuse; micro-copy is reassuring, not alarming.

---

## 2. Color palette

### Brand accent (teal/green)
Trustworthy teal — green enough to read as health, blue enough to read as calm.

| Token | Light | Dark |
|---|---|---|
| `--color-accent` | `#0E7C66` (5.1:1 on white) | `#4DC0A5` (6.7:1 on dark bg) |
| `--color-accent-hover` | `#0B5F4E` | `#63D0B4` |
| `--color-accent-text` (accent-colored text on light) | `#0B5F4E` | `#4DC0A5` |
| `--color-accent-subtle` (tinted fill for chips/badges) | `#E3F1EC` | `#123029` |

`--color-accent-text` is the darker variant used whenever the accent appears as
*text* on a light surface (links, active tab labels, stat values) to guarantee
AA. The raw `--color-accent` is reserved for filled surfaces (buttons) and
accent-colored bars/sparklines where large-area contrast is sufficient.

### Neutral scale (soft grays)
The entire layout, surfaces, borders, and body text are neutral gray.

| Role | Token | Light | Dark |
|---|---|---|---|
| App background | `--color-bg` | `#F4F6F8` | `#0D1114` |
| Raised surface / card | `--color-surface` | `#FFFFFF` | `#151B20` |
| Surface elevated (popover/menu) | `--color-surface-raised` | `#FFFFFF` | `#1D252B` |
| Subtle fill (rows, hover, chips) | `--color-surface-subtle` | `#F1F3F5` | `#1C2429` |
| Border | `--color-border` | `#D5DAE0` | `#2A333A` |
| Border strong (focus, dividers) | `--color-border-strong` | `#AEB6BF` | `#3D4850` |
| Primary text | `--color-text` | `#1A1D21` (17.5:1) | `#E7EBEE` (15:1) |
| Secondary text | `--color-text-secondary` | `#4A5158` (8.1:1) | `#C2CBD1` (11:1) |
| Muted / caption text | `--color-text-muted` | `#66707A` (5:1) | `#8B95A0` (7:1) |
| Inverse text (on accent buttons) | `--color-text-inverse` | `#FFFFFF` | `#0D1114` |

> Contrast ratios above are computed on their respective surface. Every muted
> text token still clears 4.5:1 so muted is **never** unreadable — use it for
> captions and helper text, not for critical values.

### Semantic set
Used for status and data state only. On light surfaces the text variant is a
darker shade that clears AA; the tint is for badges/chips.

| Meaning | Token | Light text | Light tint bg | Dark text | Dark tint bg |
|---|---|---|---|---|---|
| Success / in range / goal met | `--color-success` | `#1E7B34` | `#E6F4EA` | `#58C078` | `#123321` |
| Warning / caution / near threshold | `--color-warning` | `#8A5A00` | `#FBF0DB` | `#E0A93C` | `#3A2E12` |
| Danger / out of range / alert | `--color-danger` | `#B02A20` | `#FCEBEA` | `#F07B71` | `#3B1513` |
| Info / neutral guidance | `--color-info` | `#14508C` | `#E6F0F9` | `#66A9E0` | `#12293E` |
| Missing / no data | `--color-missing` | `#8A9199` | `#F0F2F4` | `#9AA3AC` | `#242B31` |

**Missing treatment is mandatory:** missing data renders as **dashed/outlined**
and colored `--color-missing`. It is never a `0` and never a flat line at zero.

### Data-viz series
All accessible color pairs (each ≥ 3:1 against both surfaces) for charts:

| Series | Token | Value |
|---|---|---|
| Primary series (e.g. steps) | `--color-accent` | `#0E7C66` |
| Secondary series (e.g. sleep) | `--color-chart-secondary` | `#3A7FB7` |
| Tertiary series (e.g. weight) | `--color-chart-tertiary` | `#7B6CB8` |
| Goal / target reference | `--color-goal` | `#2A3A4A` |
| Missing segment | `--color-missing` | dashed |

---

## 3. Typography scale

System font stack (no webfont dependency), a humanist sans that reads cleanly
at small sizes. Tabular figures (`font-variant-numeric: tabular-nums`) for all
numeric values so stat tiles don't jitter.

| Token | Value | Use |
|---|---|---|
| `--font-family` | system stack | body + UI |
| `--font-family-mono` | ui-monospace / SFMono | timestamps, codes |
| `--text-xs` | `12px / 1.4` | small caps, footnotes (non-essential) |
| `--text-sm` | `13px / 1.45` | secondary, captions, badges |
| `--text-base` | `16px / 1.5` | body, inputs (default) |
| `--text-lg` | `18px / 1.4` | list titles, form labels' emphasis |
| `--text-xl` | `22px / 1.3` | section headings |
| `--text-2xl` | `28px / 1.25` | page titles |
| `--text-stat` | `34px / 1.1` | hero stat value (tabular-nums) |
| `--weight-regular` | `400` | body |
| `--weight-medium` | `500` | labels, emphasis |
| `--weight-semibold` | `600` | card titles |
| `--weight-bold` | `700` | stat values, brand |

**Type rules**
- Body default is `--text-base` (16px). iOS inputs also use 16px to prevent
  zoom-on-focus.
- Stat values use `--text-stat` + `--weight-bold` + `tabular-nums`.
- Headings use `--weight-semibold`, not black weight — keeps it clinical.
- Never letter-space body text; use `0.02em` uppercase only for micro-labels.

---

## 4. Spacing scale

4px base grid. Use only these steps.

| Token | Value | Use |
|---|---|---|
| `--space-0` | `0` | reset |
| `--space-1` | `4px` | micro gaps, icon gutters |
| `--space-2` | `8px` | tight gaps, badge padding |
| `--space-3` | `12px` | list item padding, gap between small controls |
| `--space-4` | `16px` | default card padding, form row gap |
| `--space-5` | `20px` | section spacing, larger buttons |
| `--space-6` | `24px` | card-to-card vertical rhythm |
| `--space-8` | `32px` | page section breaks, empty-state padding |
| `--space-12` | `48px` | large empty states, page header bottom |
| `--space-16` | `64px` | hero spacing on large screens |

---

## 5. Radii, borders, shadows, elevation

| Token | Value | Use |
|---|---|---|
| `--radius-sm` | `8px` | buttons, inputs, badges |
| `--radius-md` | `12px` | stat tiles, list items, chips |
| `--radius-lg` | `16px` | cards |
| `--radius-xl` | `24px` | modals, sheets, empty-state containers |
| `--radius-full` | `999px` | pills, nav tabs, progress |

| Token | Value | Use |
|---|---|---|
| `--border-width` | `1px` | default border |
| `--border-width-strong` | `2px` | focus rings, selected states |
| `--border-style` | `solid` | default |
| `--border-style-dashed` | `dashed` | **missing data only** |

**Elevation (shadow) scale** — soft, diffuse, low-ambient:

| Token | Value | Use |
|---|---|---|
| `--shadow-sm` | `0 1px 2px rgba(0,0,0,0.06)` | resting cards on bg |
| `--shadow-md` | `0 2px 8px rgba(0,0,0,0.08)` | raised cards, sticky bars |
| `--shadow-lg` | `0 8px 24px rgba(0,0,0,0.10)` | popovers, dropdowns, sheets |
| `--shadow-focus` | `0 0 0 3px <accent 25%>` | visible focus ring |

Elevation is driven by shadows + surface tint, **never** by darkening the card.
Use at most 3 levels per screen; most screens need only `sm` and `md`.

---

## 6. Component patterns

### Card
Default content container.
- `background: var(--color-surface)`, `border: 1px solid var(--color-border)`,
  `border-radius: var(--radius-lg)`, `padding: var(--space-4)`,
  `box-shadow: var(--shadow-sm)`.
- Card header: title (`--text-lg`/`--weight-semibold`) left, optional action
  (`--color-accent-text`, `--text-sm`) right.
- Cards for the same category share equal width/height (alignment builds trust).

### Stat tile
Emphasizes one number. Value uses `--text-stat`, `--weight-bold`,
`tabular-nums`, colored:
- Healthy/in-range → `--color-text`
- Goal-met / positive → `--color-success-text`
- Out-of-range / alert → `--color-danger-text`
- **Missing → `--` in `--color-text-muted`, plus a `--color-missing` dashed
  underline hint. Never show 0.**

Include unit (`--text-sm`/`--text-muted`) and an optional delta
(`--text-sm`, success/danger). Non-interactive by default.

### Input / form field
- Container: `label` (`--text-sm`/`--weight-medium`) above a field.
- Field: `background: var(--color-surface)`, `border: 1px solid
  var(--color-border-strong)`, `border-radius: var(--radius-sm)`, padding
  `var(--space-2) var(--space-3)`, `font-size: var(--text-base)`.
- Focus: `border-color: var(--color-accent)` + `box-shadow: var(--shadow-focus)`.
- Error: `border-color: var(--color-danger)` + helper text in
  `--color-danger-text`.
- Helper/caption below field in `--text-sm`/`--text-muted`.

### Button
- Primary: `background: var(--color-accent)`, `color: var(--color-text-inverse)`,
  `border-radius: var(--radius-sm)`, padding `var(--space-2) var(--space-4)`.
  Hover → `--color-accent-hover`. Disabled → 50% opacity + `not-allowed`.
- Secondary: transparent, `border: 1px solid var(--color-border-strong)`,
  `color: var(--color-text)`.
- Ghost/link: `color: var(--color-accent-text)`, no border.
- Focus ring: `--shadow-focus`. Full-width on mobile for primary actions.

### Navigation tabs
- Horizontal pill tabs. Active tab: `background: var(--color-accent-subtle)`,
  `color: var(--color-accent-text)`. Inactive: `color: var(--color-text-secondary)`,
  transparent.
- Each tab carries an `aria-current="page"`/`role="tab"` + selected state; rely
  on both color **and** weight (not color alone) to indicate selection.

### Status badge
Small pill communicating data state.
- `background: <semantic tint>`, `color: <semantic text>`,
  `border-radius: var(--radius-full)`, padding `var(--space-1) var(--space-2)`,
  `font-size: var(--text-sm)`.
- Variants map to the semantic set (success/warning/danger/info).
- **Missing:** `background: var(--color-missing-tint)`,
  `border: 1px dashed var(--color-missing)`, `color: var(--color-text-muted)`,
  label like "No data".

### Empty state
For lists/charts with no rows.
- Centered `padding: var(--space-12)` container (`--radius-xl`),
  muted icon, title (`--text-lg`/`--weight-semibold`, `--color-text`), body
  copy (`--text-base`/`--color-text-secondary`), and a single primary CTA.
- Tone is encouraging ("No meals logged yet — add your first."), never
  alarming.

### Missing-data treatment (cross-cutting)
Any data slot without a recorded value renders:
- Value → `–` (en dash) in `--color-text-muted`;
- A dashed border or dashed outline via `--color-missing` (badge, chart cell,
  tile underline);
- Charts → a dashed gap or a dashed placeholder segment, never a zero-height bar.

---

## 7. Data-viz guidance

- **Bar charts:** solid `--color-accent` bars; baseline at 0; goal line as a
  thin dashed `--color-goal` reference. Missing day → a *dashed outline* bar at
  the adjacent bar height with `--color-missing` fill (labeled "no data"), not
  an empty column.
- **Sparklines:** single thin `--color-accent` line over `--color-surface` with
  a subtle gradient fill. Keep ≤ 40px tall, no gridlines.
- **Trend lines / line charts:** one series = `--color-accent`. Add series use
  `--chart-secondary` / `--chart-tertiary`. Legend + labels in
  `--color-text-secondary`, `--text-sm`. Missing period → dash the line
  (`--color-missing`), never connect across the gap.
- **Accessible chart colors:** all series and the missing color clear 3:1
  against both surfaces. Never encode meaning by color alone — pair series with
  shape/line-style and always provide a text-equivalent value.
- Annotate abnormal points (danger) with the danger color *and* a label/tooltip.

---

## 8. Accessibility notes

- **Contrast:** all text tokens ≥ 4.5:1 on their surface (AA normal); stat
  values and UI icons ≥ 3:1 (AA large). Checked against both themes.
- **Focus:** every interactive element has `--shadow-focus` on
  `:focus-visible`; never remove the outline.
- **Touch targets:** ≥ 44px tall (buttons, list rows, nav tabs); icon buttons
  ≥ 44×44.
- **State never by color alone:** selection, alerts, and status always pair
  color with text/weight/icon. Red is never the only signal for danger.
- **Reduced motion:** disable transitions/animations under
  `prefers-reduced-motion: reduce`.
- **Form fields:** real `<label>` for every input; `aria-describedby` for
  helper/error text; error messages programmatically associated with the field.
- **Semantic HTML:** use native `<button>`, `<a>`, `<nav>`, `<table>` for
  charts-as-data, and `role="list"` where lists are non-semantic.
- **Theme switching:** toggle `data-theme="dark"` on `<html>`; default to the
  user's `prefers-color-scheme`, persist the override. No layout shift when
  switching.
