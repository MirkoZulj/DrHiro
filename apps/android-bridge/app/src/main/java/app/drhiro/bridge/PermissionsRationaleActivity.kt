package app.drhiro.bridge

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.drhiro.bridge.ui.theme.DrHiroTheme

/**
 * Android 14 permission-rationale / usage entry point.
 *
 * Target of the ViewHealthPermissionUsage activity-alias
 * (android.intent.action.VIEW_PERMISSION_USAGE, HEALTH_PERMISSIONS category).
 * Shows a short screen explaining what the bridge reads, with a button to
 * launch the Health Connect permission request.
 */
class PermissionsRationaleActivity : ComponentActivity() {

    private val permissionLauncher = registerForActivityResult(
        androidx.health.connect.client.PermissionController
            .createRequestPermissionResultContract()
    ) { granted ->
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            DrHiroTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    Column(
                        modifier = Modifier.fillMaxSize().padding(24.dp),
                        horizontalAlignment = Alignment.CenterHorizontally,
                        verticalArrangement = Arrangement.Center,
                    ) {
                        Text(
                            "drHiro Bridge reads your health data (steps, heart rate, sleep, weight, blood pressure) so it can sync it to your drHiro account.",
                            style = MaterialTheme.typography.bodyLarge,
                        )
                        Spacer(Modifier.height(16.dp))
                        Button(onClick = {
                            permissionLauncher.launch(HealthConnectPermissions.PERMISSIONS)
                        }) { Text("Grant permissions") }
                    }
                }
            }
        }
    }
}
