package app.drhiro.bridge

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts

/**
 * Android 14 Health Connect onboarding entry point.
 *
 * This activity is the target of the `HealthConnectOnboarding` activity-alias
 * (android.health.connect.action.SHOW_ONBOARDING). When the user taps
 * "Connect" next to drHiro Bridge in Health Connect's app list, this activity
 * launches the real permission request.
 */
class OnboardingActivity : ComponentActivity() {

    private val permissionLauncher = registerForActivityResult(
        androidx.health.connect.client.PermissionController
            .createRequestPermissionResultContract()
    ) { granted ->
        // User finished (or skipped) the permission sheet; nothing more needed.
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        permissionLauncher.launch(HealthConnectPermissions.PERMISSIONS)
    }
}
