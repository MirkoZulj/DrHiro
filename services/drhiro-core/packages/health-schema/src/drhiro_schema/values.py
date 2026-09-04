"""Pydantic value schemas for metric `value_json` payloads.

Every measurements.value_json must validate against the schema for its
metric_type. New record types are added here, never in the API layer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class BloodPressureValue(BaseModel):
    systolic_mmhg: int = Field(ge=40, le=300)
    diastolic_mmhg: int = Field(ge=20, le=200)
    pulse_bpm: int | None = Field(default=None, ge=20, le=250)
    body_position: str | None = None  # sitting, standing, lying
    measurement_location: str | None = None  # left_upper_arm, right_wrist, ...

    @model_validator(mode="after")
    def _systolic_above_diastolic(self):
        if self.systolic_mmhg <= self.diastolic_mmhg:
            raise ValueError("systolic must be greater than diastolic")
        return self


class WeightValue(BaseModel):
    weight_kg: float = Field(ge=20, le=400)
    body_fat_pct: float | None = Field(default=None, ge=3, le=70)


class StepsValue(BaseModel):
    count: int = Field(ge=0, le=200_000)


class DistanceValue(BaseModel):
    distance_m: float = Field(ge=0, le=200_000)


class ActiveCaloriesValue(BaseModel):
    kcal: float = Field(ge=0, le=10_000)


class HeartRateValue(BaseModel):
    bpm: int = Field(ge=20, le=250)
    sample_type: str | None = None  # resting, active


class SleepValue(BaseModel):
    duration_min: int = Field(ge=0, le=24 * 60)
    deep_min: int | None = None
    light_min: int | None = None
    rem_min: int | None = None
    awake_min: int | None = None
    stages: list[dict] | None = None


class SpO2Value(BaseModel):
    percent: float = Field(ge=50, le=100)


class WaterValue(BaseModel):
    amount_ml: int = Field(ge=0, le=10_000)


class ExerciseValue(BaseModel):
    exercise_type: str
    duration_min: int = Field(ge=0, le=24 * 60)
    kcal: float | None = None


VALUE_SCHEMAS = {
    "steps": StepsValue,
    "distance": DistanceValue,
    "active_calories": ActiveCaloriesValue,
    "heart_rate": HeartRateValue,
    "resting_heart_rate": HeartRateValue,
    "sleep": SleepValue,
    "spo2": SpO2Value,
    "weight": WeightValue,
    "blood_pressure": BloodPressureValue,
    "water": WaterValue,
}
