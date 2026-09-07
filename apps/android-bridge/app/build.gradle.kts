plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "app.drhiro.bridge"
    compileSdk = 36

    defaultConfig {
        applicationId = "app.drhiro.bridge"
        minSdk = 28   // Health Connect requires API 28+ (Android 9)
        targetSdk = 35
        versionCode = 14
        versionName = "0.1.14"
    }

    signingConfigs {
        create("release") {
            val storeFileEnv = System.getenv("DRHIRO_STORE_FILE")
            if (storeFileEnv != null) {
                storeFile = file(storeFileEnv)
                storePassword = System.getenv("DRHIRO_STORE_PASS")
                keyAlias = System.getenv("DRHIRO_KEY_ALIAS")
                keyPassword = System.getenv("DRHIRO_KEY_PASS")
            }
        }
    }

    buildTypes {
        release {
            // Minify/R8 disabled: the release build crashed on launch with
            // isMinifyEnabled=true and no proguard rules (the Health Connect
            // aggregation classes or Compose were being stripped). The
            // previously-working install was the non-minified debug 0.1.11.
            // Disabling minify produces a reliable, signed release APK.
            isMinifyEnabled = false
            signingConfig = if (System.getenv("DRHIRO_STORE_FILE") != null) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
}

dependencies {
    implementation(platform("androidx.compose:compose-bom:2024.06.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.0")

    // Health Connect - current stable (Oct 2025) has the API-34 permission
    // contract fix. Requires SDK 36 + AGP 8.9.1 (toolchain upgraded to match).
    implementation("androidx.health.connect:connect-client:1.1.0")

    // WorkManager for periodic sync
    implementation("androidx.work:work-runtime-ktx:2.9.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    // Networking + JSON
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")
}
