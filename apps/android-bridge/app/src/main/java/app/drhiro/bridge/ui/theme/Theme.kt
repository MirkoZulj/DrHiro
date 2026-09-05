package app.drhiro.bridge.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val LightColors = lightColorScheme(
    primary = Color(0xFF2D6CDF),
    onPrimary = Color.White,
    secondary = Color(0xFF00897B),
    error = Color(0xFFC0392B),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF7FA8F0),
    onPrimary = Color.Black,
    secondary = Color(0xFF4DB6AC),
    error = Color(0xFFEF9A9A),
)

@Composable
fun DrHiroTheme(darkTheme: Boolean = false, content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        content = content,
    )
}
