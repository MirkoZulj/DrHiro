/** Shared API response types, matching the live /api/v1 contracts. */

export interface BloodPressure {
  systolic_mmhg: number
  diastolic_mmhg: number
  pulse_bpm?: number
}

export interface SleepSummary {
  duration_min: number
}

export interface Coverage {
  days_with_data: number
  expected_days: number
  score: number
}

export interface LiquidsToday {
  total_ml: number
  water: number
  non_alcoholic: number
  beer: number
  wine: number
  spirits: number
  other_alcohol: number
}

export interface Today {
  steps_today: number
  steps_coverage_7d?: Coverage
  latest_bp: BloodPressure | null
  latest_weight_kg: number | { value: number | null; measured_at?: string } | null
  water_ml_today: number
  liquids_today?: LiquidsToday
  meals_logged_today: number
  last_sleep: SleepSummary | null
  missing_data_note?: string
  heart_rate_bpm?: number | null
  calories_kcal_today?: number | null
  latest_bp_measured_at?: string | null
  latest_bp_is_stale?: boolean
  latest_bp_days_old?: number | null
  latest_bp_source_provider?: string | null
  latest_weight_measured_at?: string | null
  latest_weight_is_stale?: boolean
  latest_weight_days_old?: number | null
  latest_weight_source_provider?: string | null
  last_sleep_measured_at?: string | null
  last_sleep_is_stale?: boolean
  last_sleep_days_old?: number | null
  last_sleep_source_provider?: string | null
  heart_rate_measured_at?: string | null
  heart_rate_is_stale?: boolean
  heart_rate_days_old?: number | null
  heart_rate_source_provider?: string | null
  water_measured_at?: string | null
  water_is_stale?: boolean
  water_days_old?: number | null
  water_source_provider?: string | null
  calories_measured_at?: string | null
  calories_is_stale?: boolean
  calories_days_old?: number | null
  calories_source_provider?: string | null
  steps_measured_at?: string | null
  steps_is_stale?: boolean
  steps_days_old?: number | null
  steps_source_provider?: string | null
}

export interface DailySummary {
  date: string
  steps: number
  meals: number
  calories_kcal: number
  meals_logged: string[]
  coverage: Coverage
}

export interface WeightTrend {
  start: number
  end: number
  delta: number
  direction: string
}

export interface BpAverage {
  avg_systolic: number
  avg_diastolic: number
  avg_pulse: number | null
  count: number
}

export interface WeeklySummary {
  week: string
  days_with_steps: number
  avg_steps_week: number
  weight_trend: WeightTrend | null
  bp_average: BpAverage | null
}

export interface MealItem {
  id?: string
  food_catalog_item_id?: string
  display_name?: string
  food_name?: string
  quantity?: number
  unit?: string | null
  grams: number
  source?: string
  nutrients_json?: Record<string, unknown>
}

export interface Meal {
  id: string
  eaten_at: string
  meal_type: string | null
  status: string
  input_method: string | null
  totals_json: { kcal?: number; protein_g?: number; carbs_g?: number; fat_g?: number } | null
  items: MealItem[]
}

/** An item to submit when creating a meal via POST /meals. */
export interface MealItemIn {
  food_catalog_item_id?: string | null
  display_name: string
  quantity?: number
  unit?: string | null
  grams?: number | null
}

/** GET /meals/foods/search result (id, name, calories, serving_grams + extra). */
export interface FoodSearchResult {
  id: string
  name?: string
  display_name?: string
  calories?: number
  kcal_per_100g?: number
  serving_grams?: number
  source?: string
  barcode?: string | null
}

export interface Reminder {
  id: string
  type: string
  schedule_json: Record<string, unknown>
  enabled: boolean
  timezone?: string | null
  next_due_at?: string | null
}

export interface ReminderOccurrence {
  id: string
  reminder_id?: string
  due_at?: string
  status?: string
}

export interface Goal {
  id: string
  goal_type: string
  target_json: Record<string, unknown>
  start_date: string
  end_date: string | null
  source?: string | null
  status: string
}

/** POST /goals body. */
export interface GoalCreate {
  goal_type: string
  target_json: Record<string, unknown>
  start_date?: string
  end_date?: string | null
}

/** Monthly weight trend from GET /summaries/monthly. */
export interface MonthlyWeight {
  samples: number
  first_kg: number
  last_kg: number
  delta_kg: number
}

/** Monthly blood-pressure average from GET /summaries/monthly. */
export interface MonthlyBloodPressure {
  samples: number
  avg_systolic: number
  avg_diastolic: number
  avg_pulse: number | null
}

export interface MonthlySummary {
  month: string
  weight: MonthlyWeight | null
  blood_pressure: MonthlyBloodPressure | null
}

/** One metric parsed from a manual text ingestion. */
export interface ParsedIngestItem {
  id: string
  metric_type: string
  value_json: Record<string, unknown>
  recorded_at: string
}

/** POST /ingest/manual/text result. The backend returns `measurements`, not `parsed`. */
export interface ManualTextResult {
  accepted: number
  measurements: ParsedIngestItem[]
  unparsed: string
}

export interface Consent {
  id: string
  grantor_user_id: string
  grantee_user_id: string
  scope: string
  access_level: string
}

export interface Consents {
  granted_by_me: Consent[]
  granted_to_me: Consent[]
}

/** GET /trends/{metric_type}?days=30 result. */
export interface TrendData {
  metric_type: string
  days: number
  points: TrendPoint[]
}

export interface TrendPoint {
  date: string
  value: number
}

/** Metric configuration for the Home view cards. */
export interface MetricConfig {
  key: string
  label: string
  unit: string
  icon: string
  color: string
  trendKey: string
  /** hero tiles span 2 grid columns */
  lead?: boolean
  formatValue: (v: number | null | undefined) => string
  getValue: (t: Today) => number | null | undefined
  getMeasuredAt: (t: Today) => string | null | undefined
  getIsStale: (t: Today) => boolean | undefined
  getDaysOld: (t: Today) => number | null | undefined
  getSource: (t: Today) => string | null | undefined
}
