package app.drhiro.bridge.net

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * Device-code linking with the drHiro API.
 *
 * Flow:
 *   1. User gets a device code + installation id from the web/Mini App
 *      (POST /auth/android/device-code).
 *   2. Bridge exchanges code+installation for access/refresh tokens
 *      (POST /auth/android/exchange).
 * In the current API the device-code endpoint requires a web session, so
 * the bridge receives the code + installation id via the QR/manual entry.
 */
object DeviceLinker {

    private val json = Json { ignoreUnknownKeys = true }
    private val client = OkHttpClient.Builder().build()
    private const val JSON = "application/json; charset=utf-8"

    @Serializable
    data class ExchangeResponse(
        val access_token: String,
        val refresh_token: String,
        val token_type: String = "bearer",
        val user_id: String,
    )

    @Serializable
    data class ExchangeRequest(
        val installation_id: String,
        val device_code: String,
        val device_name: String = android.os.Build.MODEL,
        val device_model: String = android.os.Build.MODEL,
    )

    data class LinkResult(val installationId: String, val accessToken: String, val refreshToken: String)

    /**
     * Exchange a device code for tokens. The installation id is generated
     * locally on first run; the API's device-code endpoint returns it for
     * the web flow, but for direct entry the bridge generates its own and
     * pairs through the exchange endpoint.
     */
    fun link(deviceCode: String, baseUrl: String = ApiClient.baseUrl): LinkResult {
        val installationId = java.util.UUID.randomUUID().toString()
        val body = json.encodeToString(
            ExchangeRequest.serializer(),
            ExchangeRequest(installation_id = installationId, device_code = deviceCode),
        )
        val request = Request.Builder()
            .url("$baseUrl/auth/android/exchange")
            .addHeader("Content-Type", JSON)
            .post(body.toRequestBody(JSON.toMediaType()))
            .build()
        client.newCall(request).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw IllegalStateException("exchange failed: ${resp.code} ${resp.body?.string()}")
            }
            val parsed = json.decodeFromString(ExchangeResponse.serializer(), resp.body?.string() ?: "{}")
            return LinkResult(installationId, parsed.access_token, parsed.refresh_token)
        }
    }
}
