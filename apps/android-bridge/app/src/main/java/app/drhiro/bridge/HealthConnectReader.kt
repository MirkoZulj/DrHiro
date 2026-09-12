package app.drhiro.bridge

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.records.BloodPressureRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.request.AggregateGroupByPeriodRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import java.time.Instant
import java.time.LocalDateTime
import java.time.Period
import java.time.ZoneId
import kotlin.reflect.KClass

/**
 * Reads user-approved Health Connect records and normalizes them into
 * the drHiro batch contract (see docs/data-dictionary.md and the API
 * endpoint POST /api/v1/ingest/health-connect/batch).
 *
 * Sync is incremental and TRACKS A SEPARATE CURSOR PER RECORD TYPE.
 * A single shared cursor is wrong: steps are continuous and always the
 * newest record, so one cursor rides up on steps and every other type
 * (weight, sleep, BP, heart rate) — whose timestamps sit behind the
 * step-advanced cursor — is silently skipped on every subsequent sync.
 * That produced "only steps ever arrive" after the first full pull.
 * Per-type cursors let each type advance independently.
 *
 * Every record type is read with FULL pageToken pagination — Health
 * Connect returns one page per call and silently truncates the rest.
 *
 * STEPS (2026-09-07): STEPS ARE READ VIA THE AGGREGATION API, NOT RAW
 * StepsRecords. The Mi Fitness app writes steps into Health Connect at
 * INCONSISTENT granularity — some days 30-minute summary buckets, some
 * days per-minute detail, some days BOTH overlapping. Uploading the raw
 * records produced daily step totals that were sometimes double-counted
 * and sometimes half (the server could not reconcile mixed granularity).
 * Reading via `aggregateGroupByPeriod(StepsRecord.COUNT_TOTAL, range,
 * Period.ofDays(1))` makes Health Connect return ONE authoritative daily
 * step total per period — the same number the Mi Band/app reports —
 * regardless of how the source wrote it. Every day lands identically.
 */
class HealthConnectReader(private val context: Context) {

    private val client: HealthConnectClient by lazy { HealthConnectClient.getOrCreate(context) }

    // Max number of daily aggregation groups per sync. Health Connect caps
    // aggregateGroupByPeriod at 5000 groups; clamping to 90 days keeps every
    // call safely under that. The cursor advances each run, so older history
    // backfills over subsequent syncs.
    private val MAX_GROUPS_DAYS: Long = 90

    /**
     * One type entry: cursor key + a closure that reads and maps every page of
     * that record type since `since`. Each closure binds its own concrete
     * record type, so no star-projection generic issues arise.
     */
    private class TypeReader(
        val key: String,
        val read: suspend (HealthConnectClient, TimeRangeFilter) -> List<Map<String, Any?>>,
    )

    /** Read every page of one record type, mapping each record to rows. */
    private suspend fun <T : Record> readAllPages(
        recordType: KClass<T>,
        range: TimeRangeFilter,
        zone: ZoneId,
        map: (T) -> List<Map<String, Any?>>,
    ): List<Map<String, Any?>> {
        val out = mutableListOf<Map<String, Any?>>()
        var pageToken: String? = null
        do {
            val response = client.readRecords(
                ReadRecordsRequest(
                    recordType = recordType,
                    timeRangeFilter = range,
                    pageToken = pageToken,
                )
            )
            response.records.forEach { out += map(it) }
            pageToken = response.pageToken
        } while (pageToken != null)
        return out
    }

    /**
     * Read steps as ONE daily aggregate per period via Health Connect's
     * aggregation API. Health Connect returns the platform-authoritative total
     * per day — consistent no matter how the source app wrote the raw records.
     * `since` is the cursor epoch; we start from the day boundary of that
     * instant to avoid cutting a day.
     */
    private suspend fun readStepsAggregated(
        range: TimeRangeFilter,
        zone: ZoneId,
        since: Long,
    ): List<Map<String, Any?>> {
        // Always slice the read from the START of the day containing `since`,
        // so a day is never split across syncs (which would duplicate or drop
        // a partial day's steps).
        val sinceInstant = Instant.ofEpochMilli(since)
        val sinceDayStartLdt = sinceInstant.atZone(zone).toLocalDate().atStartOfDay()

        // aggregateGroupByPeriod requires a TimeRangeFilter built from
        // LocalDateTime (device zone), NOT Instant. Passing Instant throws:
        // "Either use TimeRangeFilter with LocalDateTime or
        // AggregateGroupByDurationRequest".
        //
        // Cap the window to MAX_GROUPS_DAYS so we never request more than ~90
        // daily groups. Health Connect throws
        // "Number of groups must not exceed 5000" if `since` is far in the
        // past (first sync with cursor 0 = epoch would produce tens of
        // thousands of day groups). Clamping to a bounded window keeps every
        // call under the cap; the cursor advances each sync so history
        // backfills incrementally across runs.
        // When the cursor predates the MAX_GROUPS_DAYS window, read a bounded
        // window from the cursor forward rather than from (now - MAX_GROUPS_DAYS)
        // to now. Silently narrowing the start to the latest window and then
        // persisting the newest returned bucket as the cursor would leave every
        // step older than that window permanently behind the cursor — never
        // uploaded, never reachable by later syncs. Reading forward from the
        // cursor keeps the existing "advance to newest end_at" logic correct:
        // the cursor walks forward one bounded window per sync until caught up,
        // then resumes incremental reads from the cursor to the present.
        val nowLdt = LocalDateTime.now(zone)
        val maxWindowStart = nowLdt.minusDays(MAX_GROUPS_DAYS)
        val effStart = sinceDayStartLdt
        val effEnd = if (sinceDayStartLdt.isBefore(maxWindowStart)) {
            // Cap the end so we never request more than MAX_GROUPS_DAYS groups
            // in a single call. The next sync picks up from effEnd.
            sinceDayStartLdt.plusDays(MAX_GROUPS_DAYS)
        } else {
            nowLdt
        }

        val request = AggregateGroupByPeriodRequest(
            metrics = setOf(StepsRecord.COUNT_TOTAL),
            timeRangeFilter = TimeRangeFilter.between(effStart, effEnd),
            timeRangeSlicer = Period.ofDays(1),
            dataOriginFilter = emptySet(),
        )
        val grouped = client.aggregateGroupByPeriod(request)
        val out = mutableListOf<Map<String, Any?>>()
        for (bucket in grouped) {
            val startLdt = bucket.startTime
            val endLdt = bucket.endTime
            val count = bucket.result.get(StepsRecord.COUNT_TOTAL) ?: 0L
            if (count <= 0) continue
            // Health Connect's grouped periods are LocalDateTime in the device
            // zone; attach the zone so timestamps carry an offset (needed for
            // Instant.parse in the cursor logic).
            val startZoned = startLdt.atZone(zone)
            val endZoned = endLdt.atZone(zone)
            val day = startLdt.toLocalDate()
            out += mapOf(
                "source_record_id" to "steps-daily-$day",
                "record_type" to "StepsRecord",
                "start_at" to startZoned.toInstant().toString(),
                "end_at" to endZoned.toInstant().toString(),
                "source_timezone" to zone.id,
                "values" to mapOf("count" to count.toInt()),
                "device" to mapOf("manufacturer" to "Health Connect", "model" to "aggregated-daily"),
                "client_modified_at" to endZoned.toInstant().toString(),
            )
        }
        return out
    }

    private val readers: List<TypeReader> = listOf(
        TypeReader("StepsRecord") { c, range ->
            // Aggregated daily steps (see class docstring for why).
            // `range` is TimeRangeFilter.after(since); we re-derive since from
            // the cursor inside readStepsAggregated, but we need the cursor value.
            readStepsAggregated(range, _zone, _sinceByKey["StepsRecord"] ?: 0L)
        },
        TypeReader("WeightRecord") { c, range ->
            readAllPages(WeightRecord::class, range, _zone) { r ->
                listOf(
                    mapOf(
                        "source_record_id" to r.metadata.id,
                        "record_type" to "WeightRecord",
                        "start_at" to r.time.toString(),
                        "end_at" to r.time.toString(),
                        "source_timezone" to _zone.id,
                        "values" to mapOf("weight_kg" to r.weight.inKilograms),
                        "device" to mapOf("manufacturer" to (r.metadata.device?.manufacturer ?: ""), "model" to (r.metadata.device?.model ?: "")),
                        "client_modified_at" to r.metadata.lastModifiedTime.toString(),
                    )
                )
            }
        },
        TypeReader("BloodPressureRecord") { c, range ->
            readAllPages(BloodPressureRecord::class, range, _zone) { r ->
                listOf(
                    mapOf(
                        "source_record_id" to r.metadata.id,
                        "record_type" to "BloodPressureRecord",
                        "start_at" to r.time.toString(),
                        "end_at" to r.time.toString(),
                        "source_timezone" to _zone.id,
                        "values" to mapOf(
                            "systolic_mmhg" to r.systolic.inMillimetersOfMercury.toInt(),
                            "diastolic_mmhg" to r.diastolic.inMillimetersOfMercury.toInt(),
                        ),
                        "device" to mapOf("manufacturer" to (r.metadata.device?.manufacturer ?: ""), "model" to (r.metadata.device?.model ?: "")),
                        "client_modified_at" to r.metadata.lastModifiedTime.toString(),
                    )
                )
            }
        },
        TypeReader("HeartRateRecord") { c, range ->
            readAllPages(HeartRateRecord::class, range, _zone) { r ->
                r.samples.map { s ->
                    mapOf(
                        "source_record_id" to "${r.metadata.id}-${s.time}",
                        "record_type" to "HeartRateRecord",
                        "start_at" to s.time.toString(),
                        "end_at" to s.time.toString(),
                        "source_timezone" to _zone.id,
                        "values" to mapOf("bpm" to s.beatsPerMinute.toInt()),
                        "device" to mapOf("manufacturer" to (r.metadata.device?.manufacturer ?: ""), "model" to (r.metadata.device?.model ?: "")),
                        "client_modified_at" to r.metadata.lastModifiedTime.toString(),
                    )
                }
            }
        },
        TypeReader("SleepSessionRecord") { c, range ->
            readAllPages(SleepSessionRecord::class, range, _zone) { r ->
                val durationMin = (r.endTime.toEpochMilli() - r.startTime.toEpochMilli()) / 60000
                listOf(
                    mapOf(
                        "source_record_id" to r.metadata.id,
                        "record_type" to "SleepSessionRecord",
                        "start_at" to r.startTime.toString(),
                        "end_at" to r.endTime.toString(),
                        "source_timezone" to _zone.id,
                        "values" to mapOf("duration_min" to durationMin),
                        "device" to mapOf("manufacturer" to (r.metadata.device?.manufacturer ?: ""), "model" to (r.metadata.device?.model ?: "")),
                        "client_modified_at" to r.metadata.lastModifiedTime.toString(),
                    )
                )
            }
        },
        TypeReader("ExerciseSessionRecord") { c, range ->
            readAllPages(ExerciseSessionRecord::class, range, _zone) { r ->
                val durationMin = (r.endTime.toEpochMilli() - r.startTime.toEpochMilli()) / 60000
                listOf(
                    mapOf(
                        "source_record_id" to r.metadata.id,
                        "record_type" to "ExerciseSessionRecord",
                        "start_at" to r.startTime.toString(),
                        "end_at" to r.endTime.toString(),
                        "source_timezone" to _zone.id,
                        "values" to mapOf(
                            "exercise_type" to r.exerciseType.toString(),
                            "duration_min" to durationMin,
                        ),
                        "device" to mapOf("manufacturer" to (r.metadata.device?.manufacturer ?: ""), "model" to (r.metadata.device?.model ?: "")),
                        "client_modified_at" to r.metadata.lastModifiedTime.toString(),
                    )
                )
            }
        },
    )

    // Bound from the caller's zone before any reader closure runs.
    private var _zone: ZoneId = ZoneId.systemDefault()

    // Cursor values by record-type key, so the aggregated steps reader can
    // re-derive its day-start from the StepsRecord cursor.
    private var _sinceByKey: Map<String, Long> = emptyMap()

    /**
     * Read changes for every record type since its OWN cursor. Returns the
     * records to upload plus the new per-type cursors to persist. Each type's
     * cursor advances to the newest record end-time for THAT type only, so a
     * step-heavy feed no longer prevents weight/sleep/BP/heart-rate from being
     * read on later syncs.
     */
    suspend fun readChangesSince(
        cursors: Map<String, Long>,
        zone: ZoneId = ZoneId.systemDefault(),
    ): Pair<List<Map<String, Any?>>, Map<String, Long>> {
        _zone = zone
        _sinceByKey = cursors
        val records = mutableListOf<Map<String, Any?>>()
        val newCursors = cursors.toMutableMap()

        for (reader in readers) {
            val since = cursors[reader.key] ?: 0L
            val typeRecords = reader.read(client, TimeRangeFilter.after(Instant.ofEpochMilli(since)))
            records += typeRecords
            val newest = typeRecords.maxOfOrNull {
                (it["end_at"] as? String)?.let { s -> runCatching { Instant.parse(s) }.getOrNull() }?.toEpochMilli() ?: 0L
            }
            if (newest != null && newest > since) {
                newCursors[reader.key] = newest
            }
        }
        return records to newCursors
    }
}
