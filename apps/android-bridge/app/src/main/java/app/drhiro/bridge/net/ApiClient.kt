package app.drhiro.bridge.net

import android.content.Context
import app.drhiro.bridge.TokenStore
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

object ApiClient {

    private val json = Json { ignoreUnknownKeys = true }
    private val client = OkHttpClient.Builder().build()
    private const val JSON = "application/json; charset=utf-8"

    /** Base URL of the drHiro Core API. Set at runtime by the user (Settings /
     *  onboarding); no private default is baked in. */
    var baseUrl: String = ""

    @Serializable
    data class UploadResponse(
        val accepted: Int = 0,
        val duplicates: Int = 0,
        val rejected: List<Map<String, String>> = emptyList(),
    )

    @Serializable
    data class RefreshResponse(
        val access_token: String,
        val refresh_token: String,
        val token_type: String = "bearer",
        val user_id: String,
    )

    /**
     * POST /ingest/health-connect/batch with the idempotency batch_id.
     * If the access token has expired (401), refresh once and retry —
     * the user never needs to re-enter a code.
     */
    fun uploadBatch(
        context: Context,
        installationId: String,
        accessToken: String,
        batchId: String,
        records: List<Map<String, Any?>>,
    ): UploadResponse {
        var token = accessToken
        try {
            return postBatch(installationId, token, batchId, records)
        } catch (e: RefreshNeededException) {
            token = refreshAccessToken(context)
            return postBatch(installationId, token, batchId, records)
        }
    }

    private fun postBatch(
        installationId: String,
        accessToken: String,
        batchId: String,
        records: List<Map<String, Any?>>,
    ): UploadResponse {
        val body = buildJsonObject {
            put("installation_id", installationId)
            put("batch_id", batchId)
            putJsonArray("records") {
                records.forEach { record ->
                    add(record.toJsonObject())
                }
            }
        }.toString()

        val request = Request.Builder()
            .url("$baseUrl/ingest/health-connect/batch")
            .addHeader("Authorization", "Bearer $accessToken")
            .addHeader("Content-Type", JSON)
            .post(body.toRequestBody(JSON.toMediaType()))
            .build()
        client.newCall(request).execute().use { resp ->
            if (resp.code == 401) throw RefreshNeededException()
            if (!resp.isSuccessful) {
                throw IllegalStateException("upload failed: ${resp.code}")
            }
            return json.decodeFromString(UploadResponse.serializer(), resp.body?.string() ?: "{}")
        }
    }

    /** Refresh the access token using the stored refresh token. */
    private fun refreshAccessToken(context: Context): String {
        val refresh = TokenStore.refreshToken(context)
            ?: throw IllegalStateException("no refresh token stored")
        val body = buildJsonObject { put("refresh_token", refresh) }.toString()
        val request = Request.Builder()
            .url("$baseUrl/auth/refresh")
            .addHeader("Content-Type", JSON)
            .post(body.toRequestBody(JSON.toMediaType()))
            .build()
        client.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw IllegalStateException("refresh failed: ${resp.code}")
            }
            val parsed = json.decodeFromString(RefreshResponse.serializer(), resp.body?.string() ?: "{}")
            TokenStore.saveAccessToken(context, parsed.access_token)
            return parsed.access_token
        }
    }

    private class RefreshNeededException : Exception()
}

/** Convert a Map<String, Any?> into a kotlinx JsonObject for manual serialization. */
private fun Map<String, Any?>.toJsonObject(): kotlinx.serialization.json.JsonObject {
    return buildJsonObject {
        for ((key, value) in this@toJsonObject) {
            when (value) {
                null -> put(key, JsonNull)
                is Number -> put(key, JsonPrimitive(value))
                is String -> put(key, JsonPrimitive(value))
                is Boolean -> put(key, JsonPrimitive(value))
                is Map<*, *> -> put(key, (value as Map<String, Any?>).toJsonObject())
                is List<*> -> put(key, buildJsonArray {
                    value.forEach { item ->
                        when (item) {
                            is Map<*, *> -> add((item as Map<String, Any?>).toJsonObject())
                            is Number -> add(JsonPrimitive(item))
                            is String -> add(JsonPrimitive(item))
                            is Boolean -> add(JsonPrimitive(item))
                            else -> add(JsonNull)
                        }
                    }
                })
                else -> put(key, JsonPrimitive(value.toString()))
            }
        }
    }
}
