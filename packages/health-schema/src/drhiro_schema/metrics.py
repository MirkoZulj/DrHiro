"""Metric type registry and per-metric value schemas."""

from __future__ import annotations

from enum import StrEnum


class MetricType(StrEnum):
    STEPS = "steps"
    DISTANCE = "distance"
    ACTIVE_CALORIES = "active_calories"
    EXERCISE = "exercise"
    HEART_RATE = "heart_rate"
    RESTING_HEART_RATE = "resting_heart_rate"
    SLEEP = "sleep"
    SPO2 = "spo2"
    WEIGHT = "weight"
    BLOOD_PRESSURE = "blood_pressure"
    WATER = "water"
    NUTRITION = "nutrition"


class RecordingMethod(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    OCR = "OCR"
    ESTIMATED = "estimated"


class SourceProvider(StrEnum):
    HEALTH_CONNECT = "health_connect"
    OMRON = "omron"
    MI_FITNESS = "mi_fitness"
    MANUAL = "manual"
    OCR = "ocr"
    AI_ESTIMATE = "ai_estimate"


# Health Connect record type -> drHiro metric type
HEALTH_CONNECT_RECORD_MAP = {
    "StepsRecord": MetricType.STEPS,
    "DistanceRecord": MetricType.DISTANCE,
    "ActiveCaloriesBurnedRecord": MetricType.ACTIVE_CALORIES,
    "ExerciseSessionRecord": MetricType.EXERCISE,
    "HeartRateRecord": MetricType.HEART_RATE,
    "RestingHeartRateRecord": MetricType.RESTING_HEART_RATE,
    "SleepSessionRecord": MetricType.SLEEP,
    "OxygenSaturationRecord": MetricType.SPO2,
    "WeightRecord": MetricType.WEIGHT,
    "BloodPressureRecord": MetricType.BLOOD_PRESSURE,
}

# Physiologically plausible ranges used by validation and the rule engine.
# These are NOT medical thresholds; they catch data-entry errors only.
PLAUSIBLE_RANGES = {
    MetricType.STEPS: (0, 200_000),
    MetricType.DISTANCE: (0, 200_000),  # metres
    MetricType.ACTIVE_CALORIES: (0, 10_000),  # kcal
    MetricType.HEART_RATE: (20, 250),  # bpm
    MetricType.RESTING_HEART_RATE: (30, 120),
    MetricType.SPO2: (50, 100),  # %
    MetricType.WEIGHT: (20, 400),  # kg
    MetricType.BLOOD_PRESSURE: None,  # structured; validated per field
    MetricType.WATER: (0, 10_000),  # ml
}
