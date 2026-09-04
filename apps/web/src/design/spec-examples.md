# drHiro — Spec Examples

Annotated, production-ready HTML/CSS snippets that consume the tokens from
[`tokens.css`](./tokens.css). These are reference patterns — adapt the class
names to your React component naming, but keep the token usage and structure.

Import the tokens once at your app entry (Vite):

```ts
// main.tsx (or index.tsx)
import "./design/tokens.css";
```

Toggle dark theme by setting the data attribute on the document root:

```ts
document.documentElement.dataset.theme =
  window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
```

---

## 1. Stat card

Emphasizes one number. The value color depends on state; **missing renders
`–`, never `0`**.

```html
<article class="stat-card">
  <div class="stat-card__head">
    <span class="stat-card__label">Steps today</span>
    <span class="badge badge--success">Goal met</span>
  </div>
  <p class="stat-card__value" data-stat>8,412</p>
  <p class="stat-card__unit">steps · 68% of 12,000 goal</p>
  <p class="stat-card__delta">▲ 12% vs yesterday</p>
</article>

<!-- Missing variant -->
<article class="stat-card">
  <div class="stat-card__head">
    <span class="stat-card__label">Sleep duration</span>
    <span class="badge badge--missing">No data</span>
  </div>
  <p class="stat-card__value stat-card__value--missing" data-stat>–</p>
  <p class="stat-card__unit">wearable not connected</p>
</article>
```

```css
.stat-card {
  background: var(--color-surface);
  border: var(--border-width) var(--border-style) var(--card-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: var(--space-4);
  min-width: 160px;
}
.stat-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.stat-card__label {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  font-weight: var(--weight-medium);
}
.stat-card__value {
  margin: var(--space-2) 0 var(--space-1);
  font-size: var(--text-stat);
  line-height: var(--leading-tight);
  font-weight: var(--weight-bold);
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
}
.stat-card__value--missing { color: var(--color-text-muted); }
.stat-card__unit {
  font-size: var(--text-sm);
  color: var(--color-text-muted);
}
.stat-card__delta {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-success-text);
}
```

---

## 2. Meal list item

A tappable row (≥ 44px) with title, meta, and an optional meal badge.

```html
<button class="meal-item" type="button" aria-label="View breakfast details">
  <span class="meal-item__emoji" aria-hidden="true">🥣</span>
  <span class="meal-item__body">
    <span class="meal-item__name">Breakfast</span>
    <span class="meal-item__meta">Oatmeal, banana, almond milk · 420 kcal</span>
  </span>
  <span class="badge badge--success">Logged</span>
  <span class="meal-item__chevron" aria-hidden="true">›</span>
</button>

<!-- Missing variant: draft photo not yet confirmed -->
<button class="meal-item" type="button" aria-label="Draft lunch awaiting confirmation">
  <span class="meal-item__emoji" aria-hidden="true">🥗</span>
  <span class="meal-item__body">
    <span class="meal-item__name">Lunch</span>
    <span class="meal-item__meta">Photo detected · awaiting your confirmation</span>
  </span>
  <span class="badge badge--info">Draft</span>
  <span class="meal-item__chevron" aria-hidden="true">›</span>
</button>
```

```css
.meal-item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  min-height: 44px;
  background: var(--color-surface);
  border: var(--border-width) var(--border-style) var(--card-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.meal-item:hover { background: var(--color-surface-subtle); }
.meal-item__emoji { font-size: var(--text-xl); line-height: 1; }
.meal-item__body { flex: 1; min-width: 0; }
.meal-item__name {
  display: block;
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}
.meal-item__meta {
  display: block;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.meal-item__chevron { color: var(--color-text-muted); font-size: var(--text-lg); }
```

---

## 3. Trend bar chart (with missing data)

Solid accent bars; a missing day renders as a **dashed outline** placeholder at
the neighbor's height, never an empty/zero column. A dashed goal line shows the
target.

```html
<figure class="chart">
  <figcaption class="chart__title">Steps · last 7 days</figcaption>
  <div class="bar-chart" role="img"
       aria-label="Steps: Mon 6,200; Tue 8,100; Wed no data; Thu 9,400; Fri 7,700; Sat 5,200; Sun 8,800. Goal 10,000.">
    <div class="bar-chart__plot">
      <!-- missing day uses .bar--missing -->
      <div class="bar bar--missing" style="--h: 68%" data-value="No data">
        <span class="bar__label">Wed</span>
      </div>
      <!-- normal days -->
      <div class="bar" style="--h: 62%" data-value="6,200"><span class="bar__label">Mon</span></div>
      <div class="bar" style="--h: 81%" data-value="8,100"><span class="bar__label">Tue</span></div>
      <div class="bar" style="--h: 94%" data-value="9,400"><span class="bar__label">Thu</span></div>
      <div class="bar" style="--h: 77%" data-value="7,700"><span class="bar__label">Fri</span></div>
      <div class="bar" style="--h: 52%" data-value="5,200"><span class="bar__label">Sat</span></div>
      <div class="bar" style="--h: 88%" data-value="8,800"><span class="bar__label">Sun</span></div>
      <!-- goal reference line -->
      <span class="bar-chart__goal" style="--g: 100%"></span>
    </div>
  </div>
</figure>
```

```css
.bar-chart__plot {
  position: relative;
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
  height: 180px;
  padding-top: var(--space-2);
}
.bar {
  position: relative;
  flex: 1;
  height: var(--h);
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  background: var(--color-accent);
}
.bar:hover { filter: brightness(0.96); }
/* Missing: dashed outline, tinted fill, never a zero-height bar. */
.bar--missing {
  background: var(--color-missing-tint);
  border: var(--border-width) var(--border-style-dashed) var(--color-missing);
}
.bar__label {
  position: absolute;
  top: calc(100% + var(--space-1));
  left: 0;
  right: 0;
  text-align: center;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}
/* Goal reference: thin dashed line across the plot. */
.bar-chart__goal {
  position: absolute;
  left: 0;
  right: 0;
  bottom: var(--g);
  border-top: var(--border-width-strong) var(--border-style-dashed) var(--color-goal);
  opacity: 0.6;
}
```

---

## 4. Reminder row (with snooze / skip)

Compact row with primary action (snooze) and a low-emphasis alternative (skip).

```html
<li class="reminder-row">
  <span class="reminder-row__icon" aria-hidden="true">💊</span>
  <div class="reminder-row__body">
    <span class="reminder-row__name">Take blood-pressure med</span>
    <span class="reminder-row__time">Due 8:00 AM · snoozed to 8:30</span>
  </div>
  <div class="reminder-row__actions">
    <button class="btn btn--primary btn--sm" type="button">Snooze</button>
    <button class="btn btn--ghost btn--sm" type="button">Skip</button>
  </div>
</li>
```

```css
.reminder-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface);
  border: var(--border-width) var(--border-style) var(--card-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  min-height: 44px;
}
.reminder-row__icon { font-size: var(--text-xl); }
.reminder-row__body { flex: 1; min-width: 0; }
.reminder-row__name {
  display: block;
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}
.reminder-row__time {
  display: block;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}
.reminder-row__actions {
  display: flex;
  gap: var(--space-2);
  flex-shrink: 0;
}

/* Buttons (small variant used in rows) */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-1);
  min-height: 40px;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  border: none;
  font: inherit;
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
}
.btn--sm { min-height: 36px; padding: var(--space-1) var(--space-3); }
.btn--primary { background: var(--color-accent); color: var(--color-text-inverse); }
.btn--primary:hover { background: var(--color-accent-hover); }
.btn--primary:active { background: var(--color-accent-active); }
.btn--ghost { background: transparent; color: var(--color-text-secondary); }
.btn--ghost:hover { background: var(--color-surface-subtle); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
```

---

## 5. Form field

Labeled input with helper text and an error state. Uses a real `<label>` and
associates helper/error text via `aria-describedby`.

```html
<div class="field">
  <label class="field__label" for="bp-sys">Systolic (mmHg)</label>
  <input class="field__input" id="bp-sys" name="systolic" type="number"
         inputmode="numeric" min="80" max="220" value="128"
         aria-describedby="bp-sys-help" />
  <p class="field__help" id="bp-sys-help">Normal range is 90–120.</p>
</div>

<!-- Error state -->
<div class="field field--error">
  <label class="field__label" for="bp-dia">Diastolic (mmHg)</label>
  <input class="field__input" id="bp-dia" name="diastolic" type="number"
         value="999" aria-describedby="bp-dia-help bp-dia-err" />
  <p class="field__help" id="bp-dia-help">Normal range is 60–80.</p>
  <p class="field__error" id="bp-dia-err">Please enter a value between 60 and 80.</p>
</div>
```

```css
.field { margin-bottom: var(--space-4); }
.field__label {
  display: block;
  margin-bottom: var(--space-1);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}
.field__input {
  width: 100%;
  padding: var(--input-padding);
  font: inherit;
  font-size: var(--text-base);
  color: var(--color-text);
  background: var(--input-bg);
  border: var(--border-width) var(--border-style) var(--input-border);
  border-radius: var(--input-radius);
  transition: border-color var(--duration-fast) var(--ease),
              box-shadow var(--duration-fast) var(--ease);
}
.field__input::placeholder { color: var(--color-text-disabled); }
.field__input:focus { border-color: var(--color-accent); box-shadow: var(--shadow-focus); }
.field--error .field__input { border-color: var(--color-danger); }
.field--error .field__input:focus { box-shadow: var(--shadow-focus); }
.field__help {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}
.field__error {
  margin-top: var(--space-1);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-danger-text);
}
```

---

## 6. Empty state

Encouraging, single-CTA empty state for a list with no rows. Never alarming.

```html
<div class="empty-state">
  <span class="empty-state__icon" aria-hidden="true">🥗</span>
  <h2 class="empty-state__title">No meals logged yet</h2>
  <p class="empty-state__body">
    Your meal history will appear here. Snapping a quick photo makes logging
    effortless.
  </p>
  <button class="btn btn--primary" type="button">Log your first meal</button>
</div>
```

```css
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: var(--space-3);
  padding: var(--space-12) var(--space-6);
  background: var(--color-surface);
  border: var(--border-width) var(--border-style) var(--card-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
}
.empty-state__icon { font-size: var(--text-2xl); opacity: 0.7; }
.empty-state__title {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}
.empty-state__body {
  max-width: 34ch;
  margin: 0;
  font-size: var(--text-base);
  color: var(--color-text-secondary);
}
```

---

## Status badge (used across examples)

```css
.badge {
  display: inline-flex;
  align-items: center;
  padding: var(--badge-padding);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  line-height: 1.2;
  white-space: nowrap;
}
.badge--success { background: var(--color-success-tint); color: var(--color-success-text); }
.badge--warning { background: var(--color-warning-tint); color: var(--color-warning-text); }
.badge--danger  { background: var(--color-danger-tint);  color: var(--color-danger-text); }
.badge--info    { background: var(--color-info-tint);    color: var(--color-info-text); }
.badge--missing {
  background: var(--color-missing-tint);
  color: var(--color-text-muted);
  border: var(--border-width) var(--border-style-dashed) var(--color-missing);
}
```
