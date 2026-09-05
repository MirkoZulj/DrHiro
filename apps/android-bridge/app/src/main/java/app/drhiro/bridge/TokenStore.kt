package app.drhiro.bridge

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONObject

/**
 * Persistent token store for the bridge.
 *
 * The bridge links ONCE (device-code exchange), then stores its
 * installation id, access token, and refresh token here. The access
 * token is short-lived; ApiClient auto-refreshes using the refresh
 * token, so the user never re-enters a code.
 *
 * Sync cursors are stored PER RECORD TYPE (a JSON map keyed by record
 * type) so one type advancing doesn't starve the others. On first run
 * of a build that carries per-type cursors, all cursors seed to 0 so a
 * full re-read backfills types a single-cursor build silently skipped.
 */
object TokenStore {

    private const val PREFS = "drhiro_sync"
    private const val KEY_INSTALLATION = "installation_id"
    private const val KEY_ACCESS = "access_token"
    private const val KEY_REFRESH = "refresh_token"
    private const val KEY_CURSOR = "sync_cursor"            // legacy single cursor
    private const val KEY_CURSORS = "sync_cursors_json"     // per-type map

    /** All record types the bridge reads; each gets its own cursor key. */
    val CURSOR_KEYS = listOf(
        "StepsRecord",
        "WeightRecord",
        "BloodPressureRecord",
        "HeartRateRecord",
        "SleepSessionRecord",
        "ExerciseSessionRecord",
    )

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun installationId(context: Context): String? = prefs(context).getString(KEY_INSTALLATION, null)
    fun accessToken(context: Context): String? = prefs(context).getString(KEY_ACCESS, null)
    fun refreshToken(context: Context): String? = prefs(context).getString(KEY_REFRESH, null)

    fun saveTokens(context: Context, installationId: String, accessToken: String, refreshToken: String) {
        prefs(context).edit()
            .putString(KEY_INSTALLATION, installationId)
            .putString(KEY_ACCESS, accessToken)
            .putString(KEY_REFRESH, refreshToken)
            .apply()
    }

    fun saveAccessToken(context: Context, accessToken: String) {
        prefs(context).edit().putString(KEY_ACCESS, accessToken).apply()
    }

    fun isLinked(context: Context): Boolean = installationId(context) != null

    /** Per-type sync cursors (epoch millis). Seeds all to 0 on first read. */
    fun cursors(context: Context): Map<String, Long> {
        val raw = prefs(context).getString(KEY_CURSORS, null)
        if (raw != null) {
            val obj = runCatching { JSONObject(raw) }.getOrNull()
            if (obj != null) {
                val out = mutableMapOf<String, Long>()
                for (k in CURSOR_KEYS) {
                    if (obj.has(k)) out[k] = obj.getLong(k)
                }
                if (out.isNotEmpty()) return out
            }
        }
        // No per-type cursors yet (first run of this build, or a fresh install).
        // Seed EVERY type to 0 so a full re-read backfills types a single-cursor
        // build silently skipped (weight/sleep/BP/heart-rate). Steps re-sync too,
        // but the API dedupes by (user, provider, source_record_id), so no dup rows.
        return CURSOR_KEYS.associateWith { 0L }
    }

    /** Persist per-type cursors. */
    fun saveCursors(context: Context, cursors: Map<String, Long>) {
        val obj = JSONObject()
        cursors.forEach { (k, v) -> obj.put(k, v) }
        prefs(context).edit().putString(KEY_CURSORS, obj.toString()).apply()
    }

    /** Legacy single cursor — kept for display; the map is authoritative now. */
    fun cursor(context: Context): Long {
        val c = cursors(context)
        return c.values.maxOrNull() ?: prefs(context).getLong(KEY_CURSOR, 0L)
    }

    fun saveCursor(context: Context, cursor: Long) {
        // Write-through to the legacy key for compatibility; the map is the source.
        prefs(context).edit().putLong(KEY_CURSOR, cursor).apply()
    }
}
