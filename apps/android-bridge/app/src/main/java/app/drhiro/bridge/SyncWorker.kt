package app.drhiro.bridge

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import app.drhiro.bridge.net.ApiClient

/**
 * Periodic sync worker: reads Health Connect changes since each stored
 * per-type cursor, uploads in bounded batches (idempotent by batch_id),
 * and persists the new per-type cursors. Failures retry with WorkManager
 * backoff.
 *
 * The connection is established ONCE at link time; tokens persist in
 * TokenStore and auto-refresh, so this worker never re-prompts the user.
 */
class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        val ctx = applicationContext
        val installationId = TokenStore.installationId(ctx) ?: return Result.failure()
        val accessToken = TokenStore.accessToken(ctx) ?: return Result.failure()
        val cursors = TokenStore.cursors(ctx)

        return try {
            val reader = HealthConnectReader(ctx)
            val (records, newCursors) = reader.readChangesSince(cursors)
            if (records.isNotEmpty()) {
                records.chunked(200).forEachIndexed { index, chunk ->
                    val batchId = java.util.UUID.randomUUID().toString()
                    ApiClient.uploadBatch(
                        context = ctx,
                        installationId = installationId,
                        accessToken = accessToken,
                        batchId = batchId,
                        records = chunk,
                    )
                }
            }
            TokenStore.saveCursors(ctx, newCursors)
            Result.success()
        } catch (e: Exception) {
            // Preserve cursors; retry with backoff per WorkManager policy.
            Result.retry()
        }
    }
}
