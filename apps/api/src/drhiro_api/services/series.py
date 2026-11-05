"""Daily series — single source of per-day series semantics (0.9.0 part 1).

Extracted verbatim from ``routers/dashboard.py`` (trends_daily / trends_bucketed)
so the dashboards, the future analytics engine and any other consumer bucket
days identically. Pure functions over a SQLAlchemy ``Session`` — no FastAPI
imports, no HTTP shapes.

Semantics preserved (zero behaviour change):

- Device MEASUREMENTS belong to the LOCAL CALENDAR day of their own timestamp.
- SLEEP is keyed to the WAKE-UP day (``day_boundary.wake_date``, Xiaomi/Mi
  Fitness convention) and deduplicated with the overlap-aware ``_dedup_sleep``.
- STEPS are deduplicated with ``_dedup_steps`` (Health Connect 30-minute
  aggregate buckets are authoritative; detail records only gap-fill).
- WATER/liquids, meals and nutrition totals belong to the NUTRITION day
  (``day_boundary.nutrition_date``, shifted by ``day_start_hour``).
- A day with zero source records yields ``value=None`` — never 0 — except
  ``alcohol_ml``, which yields ``0.0`` when liquids were logged but none were
  alcoholic (spec §2.1).
- A soft-deleted measurement (ledger ``deleted_at``, when the column exists)
  never appears in any total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from drhiro_api.models import Activity, Meal, Measurement, User
from drhiro_api.services.day_boundary import (
    day_start_hour,
    local_calendar_date,
    nutrition_date,
    resolve_tz,
    wake_date,
)
from drhiro_schema.metrics import MetricType


@dataclass(frozen=True)
class DayValue:
    """One day of a series. ``value=None`` means "no data", never 0."""

    day: date
    value: float | None
    n: int = 0                 # number of source records that day
    source: str | None = None  # dominant source_provider that day


SeriesKey = str


# --------------------------------------------------------------------------- #
# Liquid category bucketing (moved from routers/dashboard.py — single source)
# --------------------------------------------------------------------------- #
# Stored `value_json.category` -> (canonical bucket, legacy bucket or None).
# Each millilitre is counted ONCE in the canonical buckets; the legacy keys are
# an additional view kept because the deployed web bundle's chart reads them.
LIQUID_CANONICAL_CATS = [
    "water", "coffee", "tea", "juice", "soda", "milk", "alcohol",
    "smoothie", "broth", "other",
]
LIQUID_LEGACY_CATS = [
    "water", "non_alcoholic", "beer", "wine", "spirits", "other_alcohol",
]
LIQUID_CATEGORY_MAP: dict[str, tuple[str, str | None]] = {
    "water": ("water", None),
    "juice": ("juice", "non_alcoholic"),
    "soda": ("soda", "non_alcoholic"),
    "coffee": ("coffee", "non_alcoholic"),
    "tea": ("tea", "non_alcoholic"),
    "milk": ("milk", "non_alcoholic"),
    "smoothie": ("smoothie", "non_alcoholic"),
    "broth": ("broth", "non_alcoholic"),
    "non_alcoholic": ("other", "non_alcoholic"),
    "beer": ("alcohol", "beer"),
    "wine": ("alcohol", "wine"),
    "spirits": ("alcohol", "spirits"),
    "other_alcohol": ("alcohol", "other_alcohol"),
    "alcohol": ("alcohol", "other_alcohol"),
}


def liquid_category_buckets(cat: str | None) -> tuple[str, str | None]:
    """Map a stored liquid category to (canonical bucket, legacy bucket).

    Unknown categories fall back to the non-alcoholic bucket rather than to
    water, so an unrecognised drink is never silently counted as water.
    """
    return LIQUID_CATEGORY_MAP.get(cat or "water", ("other", "non_alcoholic"))


# --------------------------------------------------------------------------- #
# Soft-delete guard (moved from routers/dashboard.py, same behaviour)
# --------------------------------------------------------------------------- #

def _ledger_soft_delete_available(db: Session) -> bool:
    """True only when the ledger soft-delete migration has been applied.

    On a schema without `measurements.deleted_at` (production today) the filter is
    omitted entirely rather than referencing a column that does not exist.
    """
    try:
        from drhiro_api.services.log_intents import schema_capabilities
        return bool(schema_capabilities(db)["ledger_deleted_at"])
    except Exception:
        return False


def _guard_soft_deleted(q, db: Session):
    """Apply the guarded ``deleted_at IS NULL`` filter when the column exists."""
    if _ledger_soft_delete_available(db):
        # A soft-deleted liquid must not appear in any daily total. Expressed as
        # guarded raw SQL because the column is not ORM-mapped (a schema without
        # it must never see it referenced).
        q = q.filter(text("measurements.deleted_at IS NULL"))
    return q


# --------------------------------------------------------------------------- #
# Dedup helpers (moved verbatim from routers/dashboard.py)
# --------------------------------------------------------------------------- #

def _dedup_sleep(records):
    """Compute total sleep from potentially overlapping records.
    Health Connect sends full sessions + sub-stages that overlap.
    Keeps only the longest coverage per time period.
    """
    if not records:
        return 0.0
    sorted_recs = sorted(records, key=lambda r: ((r.get('end_at') or r['start_at']) - r['start_at'], r['start_at']))
    total_min = 0.0
    occupied = []
    for rec in sorted_recs:
        start = rec['start_at']
        end = rec.get('end_at') or start
        dur = (rec.get('value_json') or {}).get('duration_min', 0) or 0
        uncovered = [(start, end)]
        for occ_start, occ_end in occupied:
            new_unc = []
            for u_s, u_e in uncovered:
                if occ_end <= u_s or occ_start >= u_e:
                    new_unc.append((u_s, u_e))
                else:
                    if u_s < occ_start:
                        new_unc.append((u_s, occ_start))
                    if u_e > occ_end:
                        new_unc.append((occ_end, u_e))
            uncovered = new_unc
            if not uncovered:
                break
        if not uncovered:
            continue
        unc_sec = sum((e - s).total_seconds() for s, e in uncovered)
        total_sec = (end - start).total_seconds()
        if total_sec > 0 and unc_sec > 0:
            total_min += dur * unc_sec / total_sec
            occupied.append((start, end))
    return round(total_min, 0)


def _dedup_steps(records: list[dict]) -> int:
    """Sum step counts without double-counting overlapping intervals.

    Health Connect returns the SAME steps at two levels:
      1. 30-minute AGGREGATE buckets (spans ~1799s / 29:59, matching what the
         user's band shows), and
      2. finer per-minute / per-second detail records inside those buckets.
    Summing both double-counts (observed ~13.5k vs the band's ~7.1k).

    The 30-minute aggregate buckets are authoritative — their sum matches the
    device.  So we count buckets FULLY, and only credit a detail record for
    time NOT already covered by a bucket (gap-filling).  A detail record that
    sits entirely inside a bucket contributes nothing.

    A bucket is recognised by its long span: Health Connect 30-min summaries
    land at ~1799s, just UNDER a naive 1800s (30 min) cutoff — which is exactly
    why the old code misclassified them as granular and double-counted.  We use
    a ~28-minute threshold so the real summaries are treated as buckets.
    """
    if not records:
        return 0

    BUCKET_SECS = 28 * 60  # ~30-min Health Connect aggregate summaries

    detail = [
        r for r in records
        if (r.get("end_at") or r["start_at"]) - r["start_at"] < timedelta(seconds=BUCKET_SECS)
    ]
    buckets = [
        r for r in records
        if (r.get("end_at") or r["start_at"]) - r["start_at"] >= timedelta(seconds=BUCKET_SECS)
    ]

    def _count(rec) -> float:
        # Writers use different keys: Health Connect bridge sends
        # {"count": N}, the generic data-points writer may send
        # {"value": N} or {"steps": N}. Reading only "count" turned
        # every non-"count" row into 0 steps (wave 4 steps=0 bug).
        vj = rec.get("value_json") or {}
        for k in ("count", "value", "steps"):
            if vj.get(k) is not None:
                return float(vj[k])
        return 0.0

    # Buckets are authoritative: count fully, occupy their whole interval.
    occupied: list[tuple[datetime, datetime]] = []
    total = 0
    for rec in buckets:
        start = rec["start_at"]
        end = rec.get("end_at") or start
        total += _count(rec)
        occupied.append((start, end))

    # Detail records: only credit the portion NOT covered by any bucket.
    for rec in detail:
        start = rec["start_at"]
        end = rec.get("end_at") or start
        count = _count(rec)
        if end <= start:
            # Zero-duration point record (daily total stamped at an
            # instant, e.g. Xiaomi CSV import): it has no interval to
            # prorate, so the old `total_sec > 0` guard dropped it to
            # 0. Count it fully unless a bucket already covers its
            # instant (then it is a covered detail, not a daily total).
            if any(os_ <= start < oe for os_, oe in occupied):
                continue
            total += count
            occupied.append((start, end))
            continue
        uncovered = [(start, end)]
        for occ_start, occ_end in occupied:
            new_uncovered: list[tuple[datetime, datetime]] = []
            for u_start, u_end in uncovered:
                if occ_end <= u_start or occ_start >= u_end:
                    new_uncovered.append((u_start, u_end))
                else:
                    if u_start < occ_start:
                        new_uncovered.append((u_start, occ_start))
                    if u_end > occ_end:
                        new_uncovered.append((occ_end, u_end))
            uncovered = new_uncovered
            if not uncovered:
                break
        if not uncovered:
            continue  # fully covered by buckets
        unc_sec = sum((e - s).total_seconds() for s, e in uncovered)
        total_sec = (end - start).total_seconds()
        if total_sec > 0 and unc_sec > 0:
            total += int(count * unc_sec / total_sec)
            occupied.extend(uncovered)

    return total


# --------------------------------------------------------------------------- #
# Metric key tables
# --------------------------------------------------------------------------- #

# metric key -> MetricType for plain measurement series
_MEASUREMENT_KEYS = {
    "steps": MetricType.STEPS,
    "sleep_duration_min": MetricType.SLEEP,
    "sleep_midpoint_min": MetricType.SLEEP,
    "weight_kg": MetricType.WEIGHT,
    "bp_sys": MetricType.BLOOD_PRESSURE,
    "bp_dia": MetricType.BLOOD_PRESSURE,
    "bp_pulse": MetricType.BLOOD_PRESSURE,
    "heart_rate_bpm": MetricType.HEART_RATE,
    "heart_rate_avg": MetricType.HEART_RATE,
    "resting_hr": MetricType.RESTING_HEART_RATE,
    "spo2_avg": MetricType.SPO2,
    "stress_avg": MetricType.STRESS,
    "active_minutes": MetricType.ACTIVE_MINUTES,
    "active_kcal": MetricType.ACTIVE_CALORIES,
}

# metric key -> JSON field(s) holding the numeric value
_VALUE_KEYS = {
    MetricType.STEPS: ("count", "value"),
    MetricType.WEIGHT: ("weight_kg", "value"),
    MetricType.SLEEP: ("duration_min",),
    MetricType.WATER: ("amount_ml", "volume_ml", "water_ml"),
    MetricType.HEART_RATE: ("bpm", "heart_rate", "value"),
    MetricType.RESTING_HEART_RATE: ("bpm", "value", "avg"),
    MetricType.SPO2: ("avg", "value"),
    MetricType.STRESS: ("avg", "value"),
    MetricType.ACTIVE_MINUTES: ("minutes", "value", "count"),
    MetricType.ACTIVE_CALORIES: ("value", "count"),
}

# nutrition keys -> totals_json field
_NUTRITION_KEYS = {
    "kcal_in": "kcal",
    "calories_kcal": "kcal",
    "calories": "kcal",
    "protein_g": "protein_g",
    "carbs_g": "carbs_g",
    "fat_g": "fat_g",
}


def supported_keys() -> list[str]:
    """All series keys ``daily_series`` accepts."""
    return sorted(set(_MEASUREMENT_KEYS) | set(_NUTRITION_KEYS) | {
        "water_ml", "liquids_total_ml", "alcohol_ml",
        "alcohol_beer_ml", "alcohol_wine_ml", "alcohol_spirits_ml",
        "meals_confirmed", "activity_kcal_logged",
    })


def _iter_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _local_window(tz, start: date, end: date) -> tuple[datetime, datetime]:
    """UTC datetimes covering local-midnight(start) .. local-midnight(end+1)."""
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start_dt, end_dt


# --------------------------------------------------------------------------- #
# daily_series
# --------------------------------------------------------------------------- #

def daily_series(
    db: Session,
    user: User,
    key: SeriesKey,
    start: date,
    end: date,
) -> list[DayValue]:
    """One value per local day in [start, end], in the user's tz.

    Semantics equal what /trends/daily/{metric} and /trends/bucketed return
    today (this module is the extracted implementation). One DB query per
    source table per call; bucketing happens in Python.
    """
    tz = resolve_tz(user.timezone)
    days = list(_iter_days(start, end))

    if key in _MEASUREMENT_KEYS:
        return _measurement_series(db, user, tz, key, days, start, end)
    if key in ("water_ml", "liquids_total_ml", "alcohol_ml") or key in _ALCOHOL_LEGACY_KEYS:
        return _liquids_series(db, user, tz, key, days, start, end)
    if key in _NUTRITION_KEYS or key == "meals_confirmed":
        return _nutrition_series(db, user, tz, key, days, start, end)
    if key == "activity_kcal_logged":
        return _activity_series(db, user, days, start, end)
    raise ValueError(f"Unsupported series key: {key}")


_ALCOHOL_CATEGORIES = {"beer", "wine", "spirits", "other_alcohol", "alcohol"}
_ALCOHOL_LEGACY_KEYS = {
    "alcohol_beer_ml": "beer",
    "alcohol_wine_ml": "wine",
    "alcohol_spirits_ml": "spirits",
}


# -- measurements (local calendar day; sleep by wake date) -------------------

def _measurement_series(db, user, tz, key, days, start, end) -> list[DayValue]:
    mt = _MEASUREMENT_KEYS[key]
    start_dt, end_dt = _local_window(tz, start, end)
    q = (db.query(Measurement)
         .filter(Measurement.user_id == user.id,
                 Measurement.metric_type == mt,
                 Measurement.start_at >= start_dt, Measurement.start_at < end_dt))
    q = _guard_soft_deleted(q, db)
    rows = q.order_by(Measurement.start_at.asc()).all()

    # Bucket raw records per day, exactly as the handlers do.
    by_day: dict[date, list] = {}
    vals_by_day: dict[date, list[float]] = {}
    for m in rows:
        if mt == MetricType.SLEEP:
            wd = wake_date(m.start_at, (m.value_json or {}).get("duration_min"),
                           getattr(m, "end_at", None), tz)
            if wd is None:
                continue
            day = wd
        else:
            day = local_calendar_date(m.start_at, tz)
        rec = {
            "start_at": m.start_at,
            "end_at": getattr(m, "end_at", None) or m.start_at,
            "value_json": m.value_json or {},
            "source_provider": getattr(m, "source_provider", None),
        }
        by_day.setdefault(day, []).append(rec)
        vj = m.value_json or {}
        v = vj.get("value")
        if v is None:
            for k in _VALUE_KEYS.get(mt, ()):  # metric-specific keys
                v = vj.get(k)
                if v is not None:
                    break
        if key == "bp_sys":
            v = vj.get("systolic_mmhg")
        elif key == "bp_dia":
            v = vj.get("diastolic_mmhg")
        elif key == "bp_pulse":
            v = vj.get("pulse_bpm")
        if v is None:
            continue
        vals_by_day.setdefault(day, []).append(float(v))

    out = []
    for d in days:
        recs = by_day.get(d, [])
        vals = vals_by_day.get(d, [])
        src = None
        if recs:
            counts: dict[str, int] = {}
            for r in recs:
                sp = r.get("source_provider")
                counts[sp] = counts.get(sp, 0) + 1
            src = max(counts, key=lambda k: counts[k])
        if not recs:
            out.append(DayValue(day=d, value=None, n=0, source=None))
            continue
        if key == "steps":
            v = float(_dedup_steps(recs))
        elif key == "sleep_duration_min":
            v = _dedup_sleep(recs)
        elif key == "sleep_midpoint_min":
            # Longest session of the wake day; midpoint minutes after local
            # midnight (may be negative for a midpoint before midnight).
            longest = max(recs, key=lambda r: (r.get("end_at") or r["start_at"]) - r["start_at"])
            dur = float((longest.get("value_json") or {}).get("duration_min") or 0)
            mid = longest["start_at"] + timedelta(minutes=dur / 2)
            local_mid = datetime.combine(d, datetime.min.time(), tzinfo=tz)
            v = (mid.astimezone(tz) - local_mid).total_seconds() / 60.0
        elif key == "weight_kg" or key == "resting_hr":
            v = vals[-1] if vals else None  # last reading of the day
        elif key in ("bp_sys", "bp_dia", "bp_pulse", "heart_rate_bpm",
                     "heart_rate_avg", "spo2_avg", "stress_avg"):
            v = sum(vals) / len(vals) if vals else None  # mean of the day
        else:  # active_minutes / active_kcal
            v = sum(vals)
        n = len(recs) if key in ("steps", "sleep_duration_min", "sleep_midpoint_min") else len(vals)
        out.append(DayValue(day=d, value=v, n=n, source=src))
    return out


# -- liquids (nutrition day) -------------------------------------------------

def _liquids_series(db, user, tz, key, days, start, end) -> list[DayValue]:
    # Liquids keep the eating-day window: shifted by day_start_hour.
    h = day_start_hour()
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=tz) + timedelta(hours=h)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz) + timedelta(hours=h)
    q = (db.query(Measurement)
         .filter(Measurement.user_id == user.id,
                 Measurement.metric_type == MetricType.WATER,
                 Measurement.start_at >= start_dt, Measurement.start_at < end_dt))
    q = _guard_soft_deleted(q, db)
    rows = q.order_by(Measurement.start_at.asc()).all()

    total_by_day: dict[date, float] = {}
    alcohol_by_day: dict[date, float] = {}
    legacy_by_day: dict[date, dict[str, float]] = {}
    n_by_day: dict[date, int] = {}
    for m in rows:
        day = nutrition_date(m.start_at, tz)
        vj = m.value_json or {}
        amount = vj.get("amount_ml") or vj.get("volume_ml") or vj.get("water_ml")
        if amount is None:
            continue
        amount = float(amount)
        cat = vj.get("category") or "water"
        total_by_day[day] = total_by_day.get(day, 0.0) + amount
        n_by_day[day] = n_by_day.get(day, 0) + 1
        # NOTE: the dashboard handlers sum ALL water-metric rows without a
        # category filter, so `water_ml` (handler parity) == `liquids_total_ml`.
        # Per-category breakdowns are exposed via alcohol_ml / alcohol_*_ml.
        if cat in _ALCOHOL_CATEGORIES:
            alcohol_by_day[day] = alcohol_by_day.get(day, 0.0) + amount
        if cat in ("beer", "wine", "spirits"):
            legacy_by_day.setdefault(day, {})
            legacy_by_day[day][cat] = legacy_by_day[day].get(cat, 0.0) + amount

    out = []
    for d in days:
        if key in ("water_ml", "liquids_total_ml"):
            # water_ml keeps handler parity: all water-metric rows, no category filter.
            v = total_by_day.get(d)
        elif key == "alcohol_ml":
            # Liquids logged but none alcoholic -> 0.0; no liquids -> None.
            v = None if d not in n_by_day else alcohol_by_day.get(d, 0.0)
        else:
            legacy_key = _ALCOHOL_LEGACY_KEYS[key]
            v = None if d not in n_by_day else legacy_by_day.get(d, {}).get(legacy_key, 0.0)
        out.append(DayValue(day=d, value=v, n=n_by_day.get(d, 0)))
    return out


# -- meals (nutrition day) ---------------------------------------------------

def _nutrition_series(db, user, tz, key, days, start, end) -> list[DayValue]:
    # Meals keep the eating-day window: shifted by day_start_hour (same window
    # the /trends/bucketed calories/macros branches use).
    h = day_start_hour()
    q = (db.query(Meal)
         .filter(Meal.user_id == user.id,
                 Meal.eaten_at >= datetime.combine(start, datetime.min.time(), tzinfo=tz) + timedelta(hours=h),
                 Meal.eaten_at < datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz) + timedelta(hours=h),
                 Meal.status != "deleted"))
    meals = q.order_by(Meal.eaten_at.asc(), Meal.id.asc()).all()

    totals_by_day: dict[date, float] = {}
    confirmed_by_day: dict[date, int] = {}
    any_by_day: dict[date, int] = {}
    for m in meals:
        day = nutrition_date(m.eaten_at, tz)
        any_by_day[day] = any_by_day.get(day, 0) + 1
        if m.status == "confirmed":
            confirmed_by_day[day] = confirmed_by_day.get(day, 0) + 1
        if key in _NUTRITION_KEYS:
            t = m.totals_json or {}
            totals_by_day[day] = totals_by_day.get(day, 0.0) + float(t.get(_NUTRITION_KEYS[key]) or 0)

    out = []
    for d in days:
        if key == "meals_confirmed":
            # A day with zero confirmed meals -> None (unknown), per spec §2.1.
            v = float(confirmed_by_day[d]) if d in confirmed_by_day else None
        else:
            # Zero confirmed meals that day -> None (unknown), never 0. Matches
            # the handlers, which leave empty buckets null.
            v = totals_by_day.get(d) if any_by_day.get(d) else None
        out.append(DayValue(day=d, value=v, n=any_by_day.get(d, 0)))
    return out


# -- manual sport activities --------------------------------------------------

def _activity_series(db, user, days, start, end) -> list[DayValue]:
    rows = (db.query(Activity)
            .filter(Activity.user_id == user.id,
                    Activity.activity_date >= start, Activity.activity_date <= end)
            .all())
    by_day: dict[date, float] = {}
    n_by_day: dict[date, int] = {}
    for a in rows:
        by_day[a.activity_date] = by_day.get(a.activity_date, 0.0) + float(a.calories_burned or 0)
        n_by_day[a.activity_date] = n_by_day.get(a.activity_date, 0) + 1
    return [DayValue(day=d, value=by_day.get(d), n=n_by_day.get(d, 0)) for d in days]
