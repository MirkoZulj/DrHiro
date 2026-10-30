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
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import java.time.Instant
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
 */
class HealthConnectReader(private val context: Context) {

    private val client: HealthConnectClient by lazy { HealthConnectClient.getOrCreate(context) }

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

    private val readers: List<TypeReader> = listOf(
        TypeReader("StepsRecord") { c, range ->
            readAllPages(StepsRecord::class, range, _zone) { r ->
                listOf(
                    mapOf(
                        "source_record_id" to r.metadata.id,
                        "record_type" to "StepsRecord",
                        "start_at" to r.startTime.toString(),
                        "end_at" to r.endTime.toString(),
                        "source_timezone" to _zone.id,
                        "values" to mapOf("count" to r.count),
                        "device" to mapOf("manufacturer" to (r.metadata.device?.manufacturer ?: ""), "model" to (r.metadata.device?.model ?: "")),
                        "client_modified_at" to r.metadata.lastModifiedTime.toString(),
                    )
                )
            }
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
