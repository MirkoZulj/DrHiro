# drHiro — Data Dictionary

Canonical tables (PostgreSQL 16). All PKs are UUID v4. All timestamps are
UTC with timezone. Tenant tables carry `user_id`.

## users
| column | type | notes |
|---|---|---|
| id | uuid PK | |
| display_name | varchar(255) | |
| timezone | varchar(64) | IANA, e.g. Europe/Zagreb |
| locale | varchar(16) | |
| date_of_birth | varchar(10) | optional, ISO date |
| sex_for_health_calculations | varchar(16) | optional, explicit |
| height_cm | float | optional |
| status | varchar(20) | active/deleted |
| created_at / updated_at | timestamptz | |

## external_identities
| column | notes |
|---|---|
| user_id | FK users |
| provider | telegram, web, android_installation |
| provider_subject | telegram id / installation id |
| verified_at | |
| UNIQUE(provider, provider_subject) | |

## device_connections
| column | notes |
|---|---|
| user_id | FK users |
| provider | health_connect, omron, manual |
| device_name / device_model | |
| external_device_id_hash | installation id hash |
| permissions_json | granted HC scopes |
| last_sync_at | |
| status | active/inactive |

## measurements
| column | notes |
|---|---|
| user_id | FK users |
| metric_type | steps, distance, active_calories, exercise, heart_rate, resting_heart_rate, sleep, spo2, weight, blood_pressure, water, nutrition |
| start_at / end_at | UTC |
| value_json | canonical per-metric schema (packages/health-schema) |
| unit | |
| source_provider | health_connect, omron, mi_fitness, manual, ocr, ai_estimate |
| source_record_id | provider's record id |
| recording_method | automatic, manual, OCR, estimated |
| confidence | 0..1 |
| UNIQUE(user_id, source_provider, source_record_id) | provenance + dedup |

Example `value_json` for blood pressure:
```json
{"systolic_mmhg":128,"diastolic_mmhg":78,"pulse_bpm":64,
 "body_position":"sitting","measurement_location":"left_upper_arm"}
```

## daily_aggregates
UNIQUE(user_id, local_date, metric_type). Precomputed sums/averages for
the user's local calendar date.

## meals / meal_items
Meals: status draft|needs_review|confirmed|deleted; input_method
text|photo|barcode|copy|recipe; totals_json; photo_asset_id.
Items: food_catalog_item_id (OFF/USDA/private), grams, nutrients_json,
source, confidence, user_corrected.

## food_catalog_items
Private drHiro catalog; user_id NULL = system food. nutrients_per_100g_json.

## reminders / reminder_occurrences
Reminders: type (bp, weight, meal, water, sync, activity, bedtime,
weekly); schedule_json (`cron` or `days`+`time` + completion window);
quiet_hours_json; escalation_policy_json.
Occurrences: status pending|sent|snoozed|completed|skipped|expired;
completed_by_record_id links to the measurement that completed it.

## goals
goal_type (weight, steps, bp, protein, water); target_json; source
user|clinician|system_suggestion; status.

## rule_definitions / alerts
Rules: code, version, jurisdiction, logic_json, severity, reviewed_by,
active_from. Alerts: rule_code/version, trigger_record_ids, severity,
status open|acknowledged|resolved, explanation_template, params_json.

## consent_grants
grantor/grantee user ids; scope activity|sleep|weight|bp|nutrition|
summaries; access_level read|write; expires_at; revoked_at. Default private.

## audit_events
actor_type user|openclaw|android|system|admin; action; resource;
metadata_json; created_at. Written for reads/writes/exports/consent/
admin access.

## ingest_batches
Idempotency records: UNIQUE(user_id, batch_id), result_json. Replaying
a batch returns the stored result; no duplicate rows.

## Provenance rule
Every displayed value can show source (provider + recording method) and
timestamp. Missing wearable data is missing, never zero.
