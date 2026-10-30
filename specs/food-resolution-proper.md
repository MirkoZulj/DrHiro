# drHiro food resolution — proper fix (implementation plan)

> Owner: Jerry (implementation) · Spec author: Jeeves · Verification: Jeeves (independent re-run)
> Trigger: user report "Coke for lunch was mislogged", then "Fix it. Proper solution."

**Goal:** a bare food word logged by the user must resolve to the *right* food — the plain,
authoritative entry for that food — not to whichever branded or qualified row happens to be
shortest, and not to a vegetable that merely shares the word.

**Evidence this plan exists (all measured on production 2026-10-04):**

| query | resolves to today | kcal/100 g | why it's wrong |
|---|---|---|---|
| `banana` | Pepper, banana, raw | 27 | a chilli, not a fruit |
| `water` | Water convolvulus, raw | 19 | a leafy vegetable |
| `bacon` | Bacon, meatless | 309 | not bacon |
| `ice cream` | ICE CREAM CONE | 309 | a cone |
| `espresso coffee` | ESPRESSO COFFEE YOGOLITE | 92 | brewed espresso is ~9 |
| `peach` | Peach nectar, canned | 50 | a drink |
| `beef` | Troyer Dist. Co beef (branded) | 129 | an arbitrary brand product |

**The root cause is not one bug — it is three, and the ranking is the least of them:**

1. **The local catalogue has no generic staples.** `foods` holds 8,156 `usda` rows — a random
   subset — plus 334 `drhiro_private` rows. There is **no** `Bananas, raw`, **no**
   `Bacon, cooked`/`Pork, cured, bacon`, **no** `Ice cream, vanilla`, **no** brewed espresso,
   **no** plain `Peach, raw`. Verified by `resolve_food(..., limit=8)` dumps. The ranker cannot
   pick a correct row that does not exist, so it returns the nearest wrong one.
2. **Selection accepts a bad match as confident** (`resolve_food` -> tier/penalty/length/name).
   `length(name)` ascending actively prefers short *qualified* names such as
   `Bacon, meatless` and `Water convolvulus,raw` over longer correct ones.
3. **Translation destroys specificity.** `_lookup_nutrients` (`routers/meals.py:250`) resolves
   against `translate_food_to_english(raw)` only, so an already-corrected item
   `Beef, tenderloin steak, raw` is re-resolved as plain `beef`, and
   `Pepper, banana, raw` was once persisted under the name `banana`.
   `_persist_food` (`routers/openclaw_tools.py:707`) stores that lossy name and hardcodes
   `is_generic=True`, which minted the 35 duplicate-name groups cleaned up in 0.1.16.

Also fixed already (do not regress): total ORDER BY on `resolve_food`, `_persist_food`
name-dedupe, 136 rows renamed to their true identity, energy==0 respected in
`_local_payload`/`_apply_local_food`. All shipped in `drhiro-api:0.1.15`/`0.1.16`.

---

## Architecture of the fix

- **A. Catalogue completeness.** Import USDA Foundation Foods (authoritative generic entries,
  ~300 core foods, complete nutrient panels) into the `usda` source, plus the specific generic
  staples below from SR Legacy. This is what makes a correct answer *possible*.
- **B. Match-quality gate.** A local match is accepted only when it is a *head-token* match with
  no unrequested qualifiers/brand. Otherwise the item falls through to the existing online
  cascade (Jev-verified, plausibility-checked) and the answer is cached.
- **C. Identity-preserving lookup.** Resolve the original name first, translation second.
  Persist the candidate's original name; mark branded candidates `is_generic=false`.
- **D. Regression suite** over the user's real logged phrases, run in CI-style before deploy.

---

## Tasks

### Task 1 — a read-only acceptance harness (do this first)

**Objective:** one command that prints query -> resolved name/source/kcal for a fixed phrase
list, so "before" and "after" are comparable and verifiable by a third party.

- Create: `/opt/drhiro/apps/api/tests/resolution_report.py`
- Model it on the throwaway script already used this session (query `foods`, print
  `resolve_food` top-3 with `data_sources.source_key` + the `energy` nutrient).
- Must import the user by id, **never** `db.query(User).first()` (that is the seed user
  `Alice`, id `75b86544-f7a3-4eea-84c2-2f7a38703a4d`; the live user is
  `0bfad360-9938-4216-8abd-b44d69e2003f`, Mirko, timezone Europe/Zagreb).

Run: `docker exec drhiro-api-1 python tests/resolution_report.py`
Expected: a table of ~25 phrases. Save the output to
`/opt/drhiro/backups/resolution-before-<ts>.txt` — it is the "before" evidence.

### Task 2 — import the missing generic staples

**Objective:** every staple below has exactly one authoritative generic row.

**Files:** Create `/opt/drhiro/scripts/import_generic_staples.py` (idempotent, read the FDC key
from `USDA_API_KEY`; insert into `foods` via the `usda` DataSource + `food_nutrients` by
`nutrient_code`).

Required staples (fetch by FDC search, prefer `dataType` = `Foundation` or `SR Legacy`,
**never** `Branded`): bananas raw; apples raw with skin; peach raw; water, tap/drinking;
coffee brewed/espresso; tea brewed; bacon cooked (pork, cured); chicken breast raw AND cooked;
rice white cooked; bread white; ice cream vanilla; beef (generic, e.g. `Beef, composite of
trimmed retail cuts, separable lean and fat, raw`); potatoes baked; tomatoes raw; cheese
cheddar; yoghurt plain; milk whole; olive oil; butter; sugar; flour white.

**Verification:** `select count(*) from foods where display_name ilike 'Bananas, raw%'` = 1;
the same for three others. Screenshot the counts.

### Task 3 — match-quality gate in `resolve_food`

**File:** `apps/api/src/drhiro_api/food_search.py` (function `resolve_food`, ~line 160-205).

**Step 1 — failing test.** Add to `apps/api/tests/test_food_resolution.py` (new file):

```python
def test_bare_word_rejects_qualified_and_branded_rows(...):
    # "bacon" must NOT return "Bacon, meatless"; "water" must NOT return "Water convolvulus"
```

**Step 2 — implement a score, not a length.** Proposed order within a tier:
1. `head_match` — the query's tokens are the name's leading tokens (after normalising
   `Category, qualifier, ...`), so `Bananas, raw` beats `Pepper, banana, raw`;
2. unrequested-qualifier penalty (extend the existing `PREPARATION_QUALIFIERS` list with
   `meatless`, `imitation`, `substitute`, `nectar`, `chips`, `cone`, `sandwich`, `babyfood`,
   `baby food`, `infant`, `formula`, `powder`, `mix`, `dry`);
3. `is_generic` true first;
4. source rank — `usda` before `drhiro_private`;
5. token count of the name (fewer unrequested words first);
6. `external_id`, `id` (already added — keep, do not drop the total order).

Plural handling: match the query token against the name token with an optional trailing `s`
so `banana` reaches `Bananas, raw`, while keeping EXACT/PREFIX/WORD/SUBSTRING tier semantics
intact. Do **not** turn a WORD match into an EXACT one.

**Step 3 — apply the gate.** If the best local candidate fails the head-match test (it is a
qualified/branded near-miss), `resolve_food` must report that rather than hand it back as
confident: return the near-miss list but let `_lookup_nutrients` fall through to the online
cascade (step 2 of that function). Add a module-level predicate
`_is_weak_head_match(query, name) -> bool` and use it in both places.

**Verification:** `docker exec drhiro-api-1 python tests/resolution_report.py` shows
`bacon -> Bacon, cooked`, `banana -> Bananas, raw`, `water -> Water, bottled, generic` or
`Water, tap`, none of them branded.

### Task 4 — original-name-first resolution

**File:** `apps/api/src/drhiro_api/routers/meals.py` (`_lookup_nutrients`, ~line 250) and
`apps/api/src/drhiro_api/routers/openclaw_tools.py` (`search_food`).

- Try `resolve_food(raw_input)` first; only if it yields nothing usable, try
  `resolve_food(translate_food_to_english(raw_input))`. Both must still be Jev-verified.
- Keep DuckDuckGo on the ORIGINAL language (already the case) and keep the composite-catalogue
  fallback last.

**Verification:** re-resolving an item named `Beef, tenderloin steak, raw` returns a beef
tenderloin row (or nothing local -> online), never a branded `beef` row.
Test name: `test_specific_name_is_not_collapsed_to_its_generic_word`.

### Task 5 — persist the truth, mark brands

**File:** `apps/api/src/drhiro_api/routers/openclaw_tools.py` (`_persist_food`, ~line 707).

- Store the candidate's **original** `display_name`, not the translation (keep the name-dedupe
  from 0.1.16 so no twins are created).
- Set `is_generic = brandOwner is None` when the candidate carries brand information; pass
  `brandOwner`/`dataType` through from `online_food_candidates` if it is not already there.
- Backfill: `UPDATE foods SET is_generic=false` for the branded `drhiro_private` rows. Their
  brand is recoverable per `external_id` from
  `https://api.nal.usda.gov/fdc/v1/food/<id>?api_key=$USDA_API_KEY` (136/136 resolved earlier;
  119 had a brandOwner). Do the same for the remaining private rows (334 total) if the run
  completes inside the FDC rate limit (1,000 requests/hour — it fits, batch and log).

**Verification:** `select is_generic, count(*) from foods where data_source_id=(private) group by 1`
show the branded rows now `false`.

### Task 6 — regression suite, wired to the acceptance list

**File:** Create `apps/api/tests/test_food_resolution.py` (Task 3's tests live here too).

Phrases = the user's own logged names (top 24 by frequency, e.g. `Egg, whole, raw, fresh`,
`Corn bread (store-bought)`, `Pepper, banana, raw`, `Kefir, lowfat, plain, LIFEWAY`,
`Beef, tenderloin steak, raw`, `Water, bottled, non-carbonated`, plus the bare words
`banana`, `bacon`, `water`, `beef`, `egg`, `chicken breast`, `ice cream`, `espresso coffee`,
`peach`, `cola`, `coke`, `salt`, `honey`).

Assertions: no resolved name is a branded row for a bare generic word; no resolved name adds
an unrequested qualifier from the extended penalty list; `coke`/`cola` -> regular cola (42);
`salt` -> `Salt, table`; `honey` -> `Honey`; results are stable over 5 consecutive calls.

### Task 7 — release

- Version bump: `drhiro-api:0.1.17-resolution-tf`.
- Tag rollback **before** building: `docker tag <current> drhiro-api:rollback-resolution-<ts>`.
- Back up `infra/docker-compose.yml`; rebuild `docker compose build api`; then
  `docker compose up -d --force-recreate api` (**api only** — never the whole stack).
- Tests: run against the DEV database ONLY (throwaway `postgres` from
  `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres`, port 5435,
  then pytest from a throwaway container of the image with `--network host` and
  `DRHIRO_DATABASE_URL` **and** `DRHIRO_TEST_DATABASE_URL` explicitly at `localhost:5435`).
  The suite calls `Base.metadata.drop_all()` — pointed at production it wipes the database.
  Tear the dev stack down afterwards.
- Verify the live bot path after deploy: from the gateway container,
  `GET http://api:8000/api/v1/tools/get_my_today_summary` with `X-Service-Token` and
  `X-Telegram-Id: 984523234` must return 200 (the MCP tool path, not just `/health`).
- Re-run `tests/resolution_report.py`, save to `/opt/drhiro/backups/resolution-after-<ts>.txt`.

---

## Acceptance criteria (the deliverable is not "code merged")

1. `resolution_report.py` before/after diff shows every row of the table at the top of this
   plan corrected, with the resolved name being an unqualified generic entry.
2. `bacon`, `banana`, `water`, `beef`, `egg`, `peach`, `ice cream`, `espresso coffee` all
   resolve to plain generic rows; `coke`/`cola` -> regular cola 42 kcal; `salt` -> 0 kcal;
   `honey` -> 304 kcal.
3. Every query in the acceptance set stable over 5 consecutive runs.
4. Full suite: no NEW failures (baseline: 21 passed, 2 failed —
   `test_reminder_completion` and `test_provenance`, both pre-existing `TypeError: can't compare
   offset-naive and offset-aware datetimes` in `_validate_measured_at`, `routers/ingest.py`).
5. `/health` 200 and the gateway tool call 200 after deploy.
6. No regression on the reclaimed 2026-10-03 Coke record (item `091e7042`: 200 g/200 ml, 84 kcal,
   meal `555e1d9b` 554 kcal).

## Open questions for the user (resolve before Task 2's staples)

- Cooked or raw for staples he weighs? Known: **rice cooked, chicken raw**. Unknown for
  **beef** and **bread/potatoes**. Ask before importing; do not assume both follow one rule.

## Risks

- Changing ranking affects EVERY lookup — hence the gate is additive (weak matches fall through)
  rather than a rewrite of the tier logic.
- FDC import must be idempotent and must never insert a `Branded` row as a staple.
- The web/MCP consumers depend on the food-match payload key set — do not change key names.