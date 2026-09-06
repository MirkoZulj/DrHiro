package app.drhiro.bridge

import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.health.connect.client.permission.HealthPermission
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import app.drhiro.bridge.net.ApiClient
import app.drhiro.bridge.net.DeviceLinker
import app.drhiro.bridge.ui.theme.DrHiroTheme
import java.util.concurrent.TimeUnit

/**
 * drHiro Bridge — main screen.
 *
 * Shows three states:
 *  1. Not linked      -> user enters the device code from the drHiro
 *                         web/Mini App, then grants Health Connect perms.
 *  2. Linked          -> sync status + buttons (permissions, sync now).
 *  3. Error           -> clear message and retry.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            DrHiroTheme {
                BridgeScreen()
            }
        }
    }
}

@Composable
fun BridgeScreen() {
    val context = LocalContext.current
    val mainHandler = remember { Handler(Looper.getMainLooper()) }

    var linked by remember { mutableStateOf(TokenStore.isLinked(context)) }
    var code by remember { mutableStateOf("") }
    var status by remember { mutableStateOf("") }
    var error by remember { mutableStateOf("") }
    var lastSync by remember { mutableStateOf(TokenStore.cursor(context)) }
    var hcStatus by remember { mutableStateOf("") }

    fun updateLastSync() {
        lastSync = TokenStore.cursor(context)
    }

    // Permission launcher MUST be registered at composition time (before the
    // activity is STARTED). Calling registerForActivityResult inside a click
    // handler throws IllegalStateException -> crash.
    val permissionLauncher = rememberLauncherForActivityResult(
        androidx.health.connect.client.PermissionController
            .createRequestPermissionResultContract()
    ) { granted ->
        status = if (granted.isNotEmpty()) {
            "Permissions granted. Sync runs in background every 15 min."
        } else {
            "No permissions granted yet."
        }
        updateLastSync()
    }

    fun checkHealthConnectStatus(): String {
        return try {
            val sdk = androidx.health.connect.client.HealthConnectClient.getSdkStatus(context)
            when (sdk) {
                androidx.health.connect.client.HealthConnectClient.SDK_AVAILABLE -> "SDK_AVAILABLE ($sdk)"
                androidx.health.connect.client.HealthConnectClient.SDK_UNAVAILABLE -> "SDK_UNAVAILABLE ($sdk) — Health Connect app not installed"
                androidx.health.connect.client.HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> "UPDATE_REQUIRED ($sdk) — Health Connect app needs update"
                else -> "UNKNOWN ($sdk)"
            }
        } catch (e: Exception) {
            "EXCEPTION: ${e.javaClass.simpleName}: ${e.message}"
        }
    }

    fun requestHealthPermissions() {
        try {
            error = ""
            hcStatus = checkHealthConnectStatus()
            val sdkStatus = androidx.health.connect.client.HealthConnectClient.getSdkStatus(context)
            if (sdkStatus == androidx.health.connect.client.HealthConnectClient.SDK_AVAILABLE) {
                status = "Opening permissions… if nothing opens, use the Health Connect button below."
                // The contract delegates to the system permission dialog on
                // API 34+; on some OEMs (Xiaomi/HyperOS) that dialog is
                // suppressed for android.permission.health.* perms. So we
                // ALWAYS offer the Health Connect settings path as well.
                permissionLauncher.launch(HealthConnectPermissions.PERMISSIONS)
            } else {
                status = ""
                error = if (sdkStatus == androidx.health.connect.client.HealthConnectClient.SDK_UNAVAILABLE) {
                    "Health Connect is not installed. Tap the Play Store button below to install it."
                } else {
                    "Health Connect needs an update. Tap the Play Store button below."
                }
            }
        } catch (e: Exception) {
            error = "Permission error: ${e.javaClass.simpleName}: ${e.message}"
            status = ""
        }
    }

    fun openHealthConnectSettings() {
        try {
            // Opens the Health Connect app's own permission management UI.
            // This path works on every device regardless of OEM dialog quirks.
            // (Literal action: HealthConnectClient.getHealthConnectSettingsAction()
            // fails to resolve as a static in this alpha artifact.)
            val intent = android.content.Intent(
                "androidx.health.ACTION_HEALTH_CONNECT_SETTINGS"
            ).apply { addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK) }
            context.startActivity(intent)
        } catch (e: Exception) {
            try {
                // Fallback: launch the package directly.
                val intent = context.packageManager.getLaunchIntentForPackage(
                    "com.google.android.apps.healthdata"
                )?.apply { addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK) }
                if (intent != null) context.startActivity(intent)
                else error = "Could not open Health Connect. Install it from Play Store."
            } catch (e2: Exception) {
                error = "Could not open Health Connect: ${e2.message}"
            }
        }
    }

    fun openPlayStore() {
        try {
            val intent = android.content.Intent(
                android.content.Intent.ACTION_VIEW,
                android.net.Uri.parse("market://details?id=com.google.android.apps.healthdata")
            ).apply { addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK) }
            context.startActivity(intent)
        } catch (e: Exception) {
            try {
                val intent = android.content.Intent(
                    android.content.Intent.ACTION_VIEW,
                    android.net.Uri.parse("https://play.google.com/store/apps/details?id=com.google.android.apps.healthdata")
                ).apply { addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK) }
                context.startActivity(intent)
            } catch (e2: Exception) {
                error = "Could not open Play Store: ${e2.message}"
            }
        }
    }

    fun diagnoseHealthConnect(): String {
        val pm = context.packageManager
        return try {
            // 1. Is the Google Health Connect app actually installed as a user app?
            val pkgInfo = pm.getPackageInfo("com.google.android.apps.healthdata", 0)
            val installed = "HC app installed: v${pkgInfo.versionName} (code ${pkgInfo.versionCode})"
            // 2. Does the permission-settings intent resolve to a real activity?
            val intent = android.content.Intent("androidx.health.ACTION_HEALTH_CONNECT_SETTINGS")
            val resolved = pm.resolveActivity(intent, 0) != null
            val resolvable = "settings intent resolves: $resolved"
            // 3. SDK status
            val sdk = androidx.health.connect.client.HealthConnectClient.getSdkStatus(context)
            "$installed\n$resolvable\nSDK status: $sdk"
        } catch (e: android.content.pm.PackageManager.NameNotFoundException) {
            "Health Connect app NOT installed as a user app.\nSDK status: ${androidx.health.connect.client.HealthConnectClient.getSdkStatus(context)}\n\nThis is the problem: the framework says available, but the Google Health Connect app is missing/stubbed on this Xiaomi."
        } catch (e: Exception) {
            "Diagnose error: ${e.javaClass.simpleName}: ${e.message}"
        }
    }

    Surface(modifier = Modifier.fillMaxSize()) {
        Column(
            modifier = Modifier.fillMaxSize().padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
        ) {
            Text("drHiro Bridge", style = MaterialTheme.typography.headlineMedium)

            if (!linked) {
                Text("Enter the device code from drHiro (ask the bot for one):", style = MaterialTheme.typography.bodyLarge)
                OutlinedTextField(
                    value = code,
                    onValueChange = { code = it.trim().take(16) },
                    label = { Text("Device code") },
                    singleLine = true,
                )
                Button(
                    enabled = code.length >= 6,
                    onClick = {
                        status = "Linking…"
                        error = ""
                        Thread {
                            try {
                                val result = DeviceLinker.link(code, ApiClient.baseUrl)
                                TokenStore.saveTokens(
                                    context,
                                    result.installationId,
                                    result.accessToken,
                                    result.refreshToken,
                                )
                                mainHandler.post {
                                    linked = true
                                    status = "Linked! Grant Health Connect permissions."
                                    updateLastSync()
                                }
                                scheduleSync(context)
                            } catch (e: Exception) {
                                mainHandler.post {
                                    error = "Linking failed: ${e.message}"
                                    status = ""
                                }
                            }
                        }.start()
                    },
                ) { Text("Link device") }
                if (status.isNotEmpty()) Text(status, color = MaterialTheme.colorScheme.primary)
                if (error.isNotEmpty()) Text(error, color = MaterialTheme.colorScheme.error)
            } else {
                Text("✓ Device linked", color = MaterialTheme.colorScheme.primary)
                Text("Last sync cursor: $lastSync", style = MaterialTheme.typography.bodySmall)
                Text(
                    "Health Connect: ${if (hcStatus.isEmpty()) "tap button to check" else hcStatus}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.secondary,
                )
                Button(
                    onClick = {
                        requestHealthPermissions()
                    },
                ) { Text("Grant Health Connect permissions") }
                Button(
                    onClick = { openHealthConnectSettings() },
                ) { Text("Open Health Connect settings (manual)") }
                Button(
                    onClick = {
                        hcStatus = diagnoseHealthConnect()
                    },
                ) { Text("Diagnose Health Connect") }
                if (error.contains("Play Store", ignoreCase = true)) {
                    Button(onClick = { openPlayStore() }) {
                        Text("Open Health Connect in Play Store")
                    }
                }
                Button(
                    onClick = {
                        status = "Syncing now…"
                        error = ""
                        Thread {
                            try {
                                // Run the sync worker logic directly (in-process).
                                kotlinx.coroutines.runBlocking {
                                    val reader = HealthConnectReader(context)
                                    val installationId = TokenStore.installationId(context)
                                    val accessToken = TokenStore.accessToken(context)
                                    val cursors = TokenStore.cursors(context)
                                    if (installationId != null && accessToken != null) {
                                        val (records, newCursors) = reader.readChangesSince(cursors)
                                        if (records.isNotEmpty()) {
                                            records.chunked(200).forEachIndexed { index, chunk ->
                                                ApiClient.uploadBatch(
                                                    context = context,
                                                    installationId = installationId,
                                                    accessToken = accessToken,
                                                    batchId = java.util.UUID.randomUUID().toString(),
                                                    records = chunk,
                                                )
                                            }
                                        }
                                        TokenStore.saveCursors(context, newCursors)
                                        mainHandler.post {
                                            status = "Synced ${records.size} records."
                                            updateLastSync()
                                        }
                                    } else {
                                        mainHandler.post { error = "Not linked. Enter device code first." }
                                    }
                                }
                            } catch (e: Exception) {
                                mainHandler.post {
                                    error = "Sync error: ${e.message}"
                                    status = ""
                                }
                            }
                        }.start()
                    },
                ) { Text("Sync now") }
                if (status.isNotEmpty()) Text(status, style = MaterialTheme.typography.bodySmall)
                if (error.isNotEmpty()) Text(error, color = MaterialTheme.colorScheme.error)
            }
        }
    }
}

/**
 * Health Connect permission set. The launcher is registered via
 * rememberLauncherForActivityResult at composition time (see BridgeScreen).
 */
object HealthConnectPermissions {

    val PERMISSIONS: Set<String> = setOf(
        HealthPermission.getReadPermission(androidx.health.connect.client.records.StepsRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.DistanceRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.ActiveCaloriesBurnedRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.ExerciseSessionRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.HeartRateRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.RestingHeartRateRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.SleepSessionRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.OxygenSaturationRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.WeightRecord::class),
        HealthPermission.getReadPermission(androidx.health.connect.client.records.BloodPressureRecord::class),
        // Historical access: without this, Health Connect limits reads to
        // 30 days before first permission grant. Required for the full
        // Mi Fitness history backfill.
        HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY,
    )
}

fun scheduleSync(context: android.content.Context) {
    val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
        .build()
    WorkManager.getInstance(context).enqueueUniquePeriodicWork(
        "drhiro_sync",
        ExistingPeriodicWorkPolicy.KEEP,
        request,
    )
}
