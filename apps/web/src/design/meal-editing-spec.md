# Meal Editing UI — Design Spec (drHiro)

Status: spec for implementation · Owner view: `apps/web/src/views/Meals.tsx`
Precedent for edit interactions: `apps/web/src/views/Reminders.tsx` (`authClient.api` + PATCH + reload pattern).
Design tokens: `apps/web/src/design/tokens.css`. All styling must consume tokens only.

## 1. Core decision: detail sheet, not inline editing

**Choice: a bottom-sheet modal ("Edit meal") opened from each meal row.** The current Meals list renders items flat inside accordion categories with no per-meal grouping; inline editing there would require restructuring the list, would crowd a phone viewport with inputs/badges, and risks accidental edits on scroll-taps. A sheet gives one focused surface where grams, match, meal type and date/time all live, matches the existing `modal-overlay` / `modal-popup` patterns already used by `FoodSearchPopup`, and keeps the list read-only and scannable. Tradeoff: one extra tap per correction vs. inline; we accept the tap because the most common correction (grams) is made fast inside the sheet (see flow below).

## 2. Component hierarchy

New files under `apps/web/src/components/`:

```
MealEditSheet            (bottom sheet; owns dirty-state + save queue)
├── SheetHeader           title "Edit {meal_type}" + close ✕
├── MetaSection
│   ├── MealTypePicker    segmented buttons breakfast/lunch/dinner/snack
│   └── DateTimeEditor    date input + time input (native mobile pickers)
├── ItemList
│   └── EditableItemRow   (one per item)
│       ├── NameRow       display_name text + confidence/corrected badge
│       ├── GramsStepper  [−] [grams input] [+] + kcal preview
│       └── RowActions    "Wrong food?" (opens FoodSearchPopup) · Remove
├── AddItemButton         opens reused FoodSearchPopup
├── StickySaveBar         Save / Discard + live meal-kcal delta
└── (reused) FoodSearchPopup
```

Modified: `Meals.tsx` — group items **by meal** (not flattened) inside category bodies; each meal gets a header line (time, total kcal) and an "Edit" affordance (44px touch target). Also add per-meal kcal display.

Reused as-is: `FoodSearchPopup.tsx`, `lib/format.ts` helpers, `authClient.api`.
New types to extend in `lib/types.ts`: add `confidence?: number` and `user_corrected?: boolean` to `MealItem`.

## 3. Per-state descriptions

- **Loading:** sheet skeleton shows three gray rows (`--color-surface-subtle`, shimmer at `--duration-normal`). List behind stays usable.
- **Empty:** no meals → existing `.empty` state gains CTA "+ Log your first meal" (existing log flow).
- **Editing/dirty:** every change is local until Save. Sticky bottom bar shows `Meal total: 812 → 966 kcal` (old struck-through muted, new in `--color-accent-text`) with Save (primary) and Discard (secondary).
- **Saving:** Save button shows spinner, controls disabled (`--color-text-disabled`); bar reads "Saving…". Individual PATCHes fire optimistically in sequence; failures roll back that field only.
- **Error:** per-row error strip below the failed row (`background: var(--color-danger-tint)`, text `var(--color-danger-text)`, `border-left: var(--border-width-strong) solid var(--color-danger)`) with Retry / Revert buttons. Sheet-level save failure keeps the sheet open — never closes on error.
- **Mid-edit save failure:** user's edits stay in local state; toast "Couldn't save — your changes are kept." User can retry or discard. Navigating away or closing the sheet while dirty triggers a confirm dialog ("Discard changes?").
- **Unmatched food (backend gap #1):** if an item's kcal is null/missing, its nutrient area shows `– kcal` styled per the missing-data rule (`--color-missing-text`, dashed underline `--color-missing`), plus a persistent warning chip "Not counted" (`--color-warning-tint` bg, `--color-warning-text`). The sticky bar appends "(1 item not counted)" so totals are never silently wrong. Fix path: re-pointing the match (below) or deleting/re-adding via search.
- **Match re-point unavailable (backend gap #2):** "Wrong food?" opens FoodSearchPopup; selecting a result calls `PATCH /meals/<id>/items/<item_id>` with the new food identity when the endpoint exists. Until then the popup renders but the row shows an info note: "Relinking coming soon — remove this item and add the correct food instead," with quick Remove prefilled. UI ships now, endpoint plugs in later.

## 4. Confidence & corrected treatment (quiet by default)

Per row, exactly one small badge after the name (`--badge-radius`, `--badge-padding`, `--text-sm`):

| State | Badge | Style |
|---|---|---|
| confidence ≥ 0.8 | none | nothing rendered |
| confidence ~0.5 | "check?" pill | `--color-warning-tint` bg, `--color-warning-text` |
| confidence ≤ 0.3 / unresolved | "unresolved" pill | `--color-danger-tint` bg, `--color-danger-text` |
| user_corrected = true | "✓ fixed" pill | `--color-success-tint` bg, `--color-success-text` |

Rules: badges are text+color (never color alone); corrected overrides the confidence badge; confident+uncorrected rows render clean so the screen is not a wall of warnings. Category headers may show a tiny count chip ("2 need review", warning-tint) only when unreviewed low-confidence items exist — tapping it expands and scrolls to them.

## 5. Primary flow: correct the grams (tap-by-tap)

1. Meals → expand a category → tap the meal's **Edit** (or the meal header). Sheet opens.
2. Tap the grams value of the item (the whole value is a 44px-tall tappable input, pre-selected numeric keypad via `inputmode="decimal"`).
3. Type new amount. The kcal preview beside the input updates live: `500 g · 890 kcal` recalculated from `kcal_per_100g` proportionally; sticky bar shows meal-total delta immediately.
4. Tap **Save**. One optimistic PATCH `/meals/<meal_id>/items/<item_id>` `{ grams }`; row badge flips to "✓ fixed"; sheet closes; list and daily summary reload via existing `loadMeals()`.

Fast paths: `[−]/[+]` steppers step ±10 g (long-press repeats), covering the wine case (100→150 ml/g) in two taps without typing. Total taps for a gram fix: 3 (Edit → stepper ×2 auto-saves? No — still requires Save, so 4 including Save).

## 6. Other flows

- **Rename/relabel:** tap name → inline text input → Save (PATCH `display_name`). Note shown if kcal won't change ("Label only — nutrition unchanged").
- **Wrong food:** row action "Wrong food?" → FoodSearchPopup (pre-filled query = current name) → pick → PATCH with new food id (pending backend gap #2 fallback above).
- **Add item:** "Add item" → FoodSearchPopup with grams input (component already supports per-result grams) → POST `/meals/<meal_id>/items`. If the picked food has no kcal data, apply the "Not counted" treatment immediately.
- **Remove:** trash icon per row → confirm only for items with kcal > 200 ("Remove 620 kcal of steak?"); DELETE endpoint; optimistic removal.
- **Meal type:** segmented control in MetaSection → PATCH `/meals/<id>` `{ meal_type }`; moving between categories reflects after save.
- **Date/time:** native `<input type="date">` + `<input type="time">` side by side → PATCH `eaten_at` ISO string. Show original parsed value pre-filled (this fixes bot misreads like "last Monday").

## 7. Accessibility & layout

Bottom sheet: max-height 85vh, scrollable content, sticky footer bar (`--shadow-md`, `background: var(--color-surface-raised)`), radius `--radius-xl`, overlay `rgba` dim. All touch targets ≥ 44px. Focus trap in sheet; Escape/overlay click = close (confirm if dirty). `role="dialog"` + `aria-label`. Numeric displays use `tabular-nums`. Reduced motion respected.

## 8. Acceptance criteria

1. Tapping Edit on a meal opens the sheet pre-filled with that meal's items, type, date, time.
2. Changing grams shows updated per-item kcal AND meal-total delta before Save; Save persists and the daily summary updates.
3. Stepper −/+ adjusts grams in 10 g steps; two taps take 100→120.
4. An item with null kcal renders "– kcal" dashed + "Not counted" chip and the save bar states it isn't counted; adding such an item never silently inflates totals.
5. Low-confidence rows show warning/danger pills; corrected rows show "✓ fixed"; confident clean rows show no badge.
6. "Wrong food?" works end-to-end once the re-point endpoint lands; before that it degrades gracefully with the remove-and-re-add guidance.
7. Add item via FoodSearchPopup creates an item; remove deletes it (with confirm for large-kcal items).
8. Changing meal type moves the meal to another category; changing date/time persists `eaten_at`.
9. Killing the network mid-save leaves the sheet open with edits intact, a per-row error strip, and working Retry/Revert.
10. Closing with unsaved changes asks for confirmation; Discard loses nothing server-side.
11. No hardcoded hex anywhere; all values come from `tokens.css` custom properties.
